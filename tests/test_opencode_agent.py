"""Test the native agent boundary without invoking a hosted model."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from hermes_gateway import opencode_agent as native
from hermes_gateway.opencode_models import discover_models
from hermes_gateway.opencode_quota import native_rate_limit


def test_child_does_not_inherit_provider_keys_or_approval_overrides(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-forward")
    monkeypatch.setenv("HERMES_HOME", "do-not-forward")
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", "do-not-forward")
    monkeypatch.setenv("HERMES_YOLO", "1")
    monkeypatch.setenv("USERPROFILE", "C:\\Users\\PC")
    env = native.child_environment()
    assert env["USERPROFILE"] == "C:\\Users\\PC"
    assert (
        not {"OPENAI_API_KEY", "HERMES_HOME", "OPENCODE_CONFIG_CONTENT", "HERMES_YOLO"} & env.keys()
    )


def test_native_tools_remain_owned_by_opencode(tmp_path, monkeypatch):
    prompts = []

    async def fake_run(settings, command, prompt):
        prompts.append(prompt)
        return "CODING_OK", {"prompt_tokens": 10, "completion_tokens": 2}, ["write"]

    monkeypatch.setattr(native, "run_agent", fake_run)
    settings = native.AgentSettings(workdir=tmp_path, log_file=tmp_path / "usage.jsonl")
    with TestClient(native.create_agent_app(settings, "unused.exe")) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": settings.alias,
                "messages": [
                    {"role": "system", "content": "Hermes must execute terminal calls"},
                    {"role": "user", "content": "Create hello.py"},
                ],
                "tools": [{"type": "function", "function": {"name": "hermes_terminal"}}],
            },
        )
    assert response.json()["choices"][0]["message"]["content"] == "CODING_OK"
    assert "tool_calls" not in response.json()["choices"][0]["message"]
    assert "Create hello.py" in prompts[0]
    assert "hermes_terminal" not in prompts[0]
    assert "Hermes must execute terminal calls" not in prompts[0]
    record = json.loads(settings.log_file.read_text())
    assert record["native_tools"] == ["write"]
    assert "Create hello.py" not in settings.log_file.read_text()


def test_agent_error_is_not_retried(tmp_path, monkeypatch):
    attempts = []

    async def fail(settings, command, prompt):
        attempts.append(prompt)
        raise RuntimeError("native agent failed")

    monkeypatch.setattr(native, "run_agent", fail)
    settings = native.AgentSettings(workdir=tmp_path, log_file=tmp_path / "usage.jsonl")
    with TestClient(native.create_agent_app(settings, "unused.exe")) as client:
        result = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "Create hello.py"}],
            },
        )
        assert result.status_code == 502
    assert len(attempts) == 1


def test_paid_model_is_rejected():
    with pytest.raises(ValueError):
        native.AgentSettings(model="opencode/paid-model")


def test_catalog_excludes_paid_models_and_selects_requested_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "hermes_gateway.opencode_models.subprocess.run",
        lambda *a, **kw: SimpleNamespace(
            returncode=0,
            stdout="opencode/glm-5.3-flash-free\nopencode/paid-model\nopencode/big-pickle\n",
        ),
    )
    catalog = discover_models("unused.exe", {}, tmp_path)
    assert set(catalog) == {"oc-glm-5.3-flash-free", "oc-big-pickle"}
    selected = []

    async def fake_run(settings, command, prompt):
        selected.append(settings.model)
        return "OK", {}, []

    monkeypatch.setattr(native, "run_agent", fake_run)
    settings = native.AgentSettings(workdir=tmp_path, log_file=tmp_path / "usage.jsonl")
    with TestClient(native.create_agent_app(settings, "unused.exe", catalog)) as client:
        assert len(client.get("/v1/models").json()["data"]) == 2
        assert (
            client.post(
                "/v1/chat/completions",
                json={
                    "model": "oc-glm-5.3-flash-free",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            ).status_code
            == 200
        )
    assert selected == ["opencode/glm-5.3-flash-free"]
    assert settings.model == "opencode/deepseek-v4.1-flash-free"


def test_live_progress_order_and_reasoning_not_exposed(tmp_path, monkeypatch):
    async def events(settings, command, prompt):
        yield {"type": "step_start", "part": {}}
        yield {"type": "reasoning", "part": {"text": "PRIVATE_THOUGHT"}}
        yield {"type": "tool_use", "part": {"tool": "write", "state": {"status": "completed"}}}
        yield {"type": "text", "part": {"text": "FINAL_OK"}}

    monkeypatch.setattr(native, "agent_events", events)
    settings = native.AgentSettings(workdir=tmp_path, log_file=tmp_path / "usage.jsonl")
    with TestClient(native.create_agent_app(settings, "unused.exe")) as client:
        result = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "Code"}],
                "stream": True,
            },
        )
    content = "".join(
        choice.get("delta", {}).get("content", "")
        for line in result.text.splitlines()
        if line.startswith("data: {")
        for choice in json.loads(line[6:]).get("choices", [])
    )
    assert content.index("Working - step 1") < content.index("Completed: write")
    assert content.index("Completed: write") < content.index("FINAL_OK")
    assert content.index("FINAL_OK") < content.index("Finished")
    assert "PRIVATE_THOUGHT" not in content
    assert "data: [DONE]" in result.text


@pytest.mark.asyncio
async def test_agent_events_yield_before_process_finishes_and_clean_up(tmp_path, monkeypatch):
    reader = asyncio.StreamReader()
    reader.feed_data(b'{"type":"step_start","part":{}}\n')
    killed = []

    async def done():
        return None

    process = SimpleNamespace(
        stdout=reader,
        stdin=SimpleNamespace(write=lambda data: None, drain=done, close=lambda: None),
        returncode=None,
    )

    async def spawn(*args, **kwargs):
        return process

    async def stop(owned):
        killed.append(owned)

    monkeypatch.setattr(native.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(native, "kill_agent", stop)
    stream = native.agent_events(native.AgentSettings(workdir=tmp_path), "unused.exe", "Code")
    first = await asyncio.wait_for(anext(stream), timeout=1)
    assert first["type"] == "step_start"
    assert process.returncode is None
    await stream.aclose()
    assert killed == [process]


def test_failed_stream_never_claims_finished_or_retries(tmp_path, monkeypatch):
    attempts = []

    async def events(settings, command, prompt):
        attempts.append(prompt)
        yield {"type": "step_start", "part": {}}
        raise RuntimeError("test failure")

    monkeypatch.setattr(native, "agent_events", events)
    settings = native.AgentSettings(workdir=tmp_path, log_file=tmp_path / "usage.jsonl")
    with TestClient(native.create_agent_app(settings, "unused.exe")) as client:
        result = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "Code"}],
                "stream": True,
            },
        )
    assert "The turn failed" in result.text
    assert "Finished" not in result.text
    assert len(attempts) == 1
    assert json.loads(settings.log_file.read_text())["status"] == "agent_error"


def test_history_removes_progress_lines_without_removing_user_text():
    prompt = native.render_conversation(
        [
            {"role": "user", "content": "[OpenCode] This is my literal message"},
            {
                "role": "assistant",
                "content": "[OpenCode] Working\nActual answer\n[OpenCode] Finished",
            },
            {"role": "user", "content": "Continue"},
        ]
    )
    assert "This is my literal message" in prompt
    assert "Actual answer" in prompt
    assert "[OpenCode] Working" not in prompt


def test_free_quota_blocks_retries_other_models_and_restart(tmp_path, monkeypatch):
    attempts = []

    async def limited(settings, command, prompt):
        attempts.append(settings.model)
        yield {"type": "step_start", "part": {}}
        raise native_rate_limit(
            {"error": {"type": "provider.free-limit", "status": 429}, "retryAfterMs": 600000}
        )

    monkeypatch.setattr(native, "agent_events", limited)
    settings = native.AgentSettings(workdir=tmp_path, log_file=tmp_path / "usage.jsonl")
    catalog = {"oc-glm-5.3-flash-free": "opencode/glm-5.3-flash-free"}
    request = {"messages": [{"role": "user", "content": "Hello"}], "stream": True}
    with TestClient(native.create_agent_app(settings, "unused.exe", catalog)) as client:
        result = client.post("/v1/chat/completions", json=request)
        assert "Free usage limit reached" in result.text
        assert "data: [DONE]" in result.text
        assert "Finished" not in result.text
    with TestClient(native.create_agent_app(settings, "unused.exe", catalog)) as restarted:
        result = restarted.post(
            "/v1/chat/completions",
            json={
                **request,
                "model": "oc-glm-5.3-flash-free",
                "stream": False,
            },
        )
        assert result.status_code == 429
        assert int(result.headers["retry-after"]) > 0
    assert attempts == ["opencode/deepseek-v4.1-flash-free"]
