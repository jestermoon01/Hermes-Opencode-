"""Check task handoff without provider traffic or shared-quota circumvention."""

import json

import pytest
from fastapi.testclient import TestClient

from hermes_gateway import opencode_agent as native
from hermes_gateway.model_router import ModelRouter
from hermes_gateway.opencode_quota import OpenCodeRateLimit, QuotaGuard, native_rate_limit
from hermes_gateway.task_context import TaskStore

FIRST = "opencode/deepseek-v4.1-flash-free"
SECOND = "opencode/glm-5.3-flash-free"
THIRD = "opencode/big-pickle"
CATALOG = {"oc-first": FIRST, "oc-second": SECOND, "oc-third": THIRD}


def settings(tmp_path, **extra):
    return native.AgentSettings(
        workdir=tmp_path,
        log_file=tmp_path / "usage.jsonl",
        task_continuity=True,
        automatic_model_fallback=True,
        **extra,
    )


def body(**extra):
    return {
        "messages": [{"role": "user", "content": "Create the task file and verify it."}],
        **extra,
    }


@pytest.mark.parametrize("stream", [False, True])
def test_model_handoff_keeps_context_session_and_partial_work(tmp_path, monkeypatch, stream):
    attempts = []

    async def events(config, command, prompt):
        attempts.append(config.model)
        if config.model == FIRST:
            assert not config.native_session_id
            (tmp_path / "task.txt").write_text("already-created")
            yield {"type": "step_start", "sessionID": "ses_continuity", "part": {}}
            yield {
                "type": "tool_use",
                "part": {
                    "tool": "write",
                    "state": {"status": "completed"},
                },
            }
            yield {"type": "text", "part": {"text": "Created task.txt; verification remains."}}
            yield {"type": "step_finish", "part": {"tokens": {"input": 10, "output": 2}}}
            raise OpenCodeRateLimit(300, model_specific=True)
        assert config.model == SECOND
        assert config.native_session_id == "ses_continuity"
        assert "verification remains" in prompt
        assert "Create the task file and verify it" in prompt
        assert "do not replay the task from the beginning" in prompt
        assert '"tool": "write", "status": "completed"' in prompt
        assert (tmp_path / "task.txt").read_text() == "already-created"
        yield {"type": "step_start", "part": {}}
        yield {"type": "text", "part": {"text": "CONTINUED_OK"}}
        yield {"type": "step_finish", "part": {"tokens": {"input": 4, "output": 1}}}

    monkeypatch.setattr(native, "agent_events", events)
    config = settings(tmp_path)
    with TestClient(native.create_agent_app(config, "unused.exe", CATALOG)) as client:
        result = client.post("/v1/chat/completions", json=body(stream=stream))
    assert result.status_code == 200
    if stream:
        assert "[OpenCode]" not in result.text
        assert "CONTINUED_OK" in result.text
        assert "Created task.txt; verification remains" not in result.text
        assert "data: [DONE]" in result.text
    else:
        assert result.json()["choices"][0]["message"]["content"] == "CONTINUED_OK"
    assert attempts == [FIRST, SECOND]
    checkpoint = json.loads(next((tmp_path / "tasks").glob("*.json")).read_text())
    assert checkpoint["status"] == "completed"
    assert checkpoint["model"] == SECOND
    assert checkpoint["payload"]["messages"][0]["content"] == body()["messages"][0]["content"]
    records = [json.loads(line) for line in config.log_file.read_text().splitlines()]
    model_records = [record for record in records if record["kind"] == "model_attempt"]
    assert [record["model"] for record in model_records] == attempts
    assert sum(record["usage"].get("prompt_tokens", 0) for record in records) == 14
    assert sum(record["usage"].get("completion_tokens", 0) for record in records) == 3


@pytest.mark.parametrize("free_limit", [False, True])
def test_shared_and_unknown_limits_stop_all_model_switching(tmp_path, monkeypatch, free_limit):
    attempts = []

    async def events(config, command, prompt):
        attempts.append(config.model)
        yield {"type": "step_start", "part": {}}
        raise OpenCodeRateLimit(600, free_limit=free_limit)

    monkeypatch.setattr(native, "agent_events", events)
    config = settings(tmp_path)
    with TestClient(native.create_agent_app(config, "unused.exe", CATALOG)) as client:
        result = client.post("/v1/chat/completions", json=body(stream=True))
        assert "Resume later with -ResumeTask" in result.text
        assert "Continuing task with" not in result.text
    with TestClient(native.create_agent_app(config, "unused.exe", CATALOG)) as client:
        result = client.post("/v1/chat/completions", json=body(model="oc-second"))
        assert result.status_code == 429
        assert result.headers["x-hermes-task-id"]
    assert attempts == [FIRST]


def test_fallback_exhaustion_is_bounded_and_never_repeats_a_model(tmp_path, monkeypatch):
    attempts = []

    async def events(config, command, prompt):
        attempts.append(config.model)
        yield {"type": "step_start", "part": {}}
        raise OpenCodeRateLimit(600, model_specific=True)

    monkeypatch.setattr(native, "agent_events", events)
    with TestClient(
        native.create_agent_app(settings(tmp_path, max_model_switches=1), "unused.exe", CATALOG)
    ) as client:
        result = client.post("/v1/chat/completions", json=body())
    assert result.status_code == 429
    assert attempts == [FIRST, SECOND]
    checkpoint = json.loads(next((tmp_path / "tasks").glob("*.json")).read_text())
    assert checkpoint["status"] == "paused_rate_limited"


def test_saved_model_cooldown_is_skipped_without_another_call(tmp_path, monkeypatch):
    QuotaGuard(tmp_path / "opencode-quota.json").record(
        FIRST, OpenCodeRateLimit(600, model_specific=True)
    )
    attempts = []

    async def events(config, command, prompt):
        attempts.append(config.model)
        yield {"type": "text", "part": {"text": "OK"}}

    monkeypatch.setattr(native, "agent_events", events)
    with TestClient(native.create_agent_app(settings(tmp_path), "unused.exe", CATALOG)) as client:
        assert client.post("/v1/chat/completions", json=body()).status_code == 200
    assert attempts == [SECOND]


def test_restart_can_resume_checkpoint_with_another_model(tmp_path, monkeypatch):
    config = settings(tmp_path)
    attempts = []

    async def events(current, command, prompt):
        attempts.append(current.model)
        if len(attempts) == 1:
            yield {"type": "step_start", "sessionID": "ses_saved", "part": {}}
            yield {"type": "text", "part": {"text": "Stopped after inspecting the task file."}}
            raise RuntimeError("Interrupted native operation")
        assert "Create the task file and verify it" in prompt
        assert "Continue verification only" in prompt
        assert "Stopped after inspecting" in prompt
        assert current.native_session_id == "ses_saved"
        yield {"type": "text", "part": {"text": "RESUMED_OK"}}

    monkeypatch.setattr(native, "agent_events", events)
    with TestClient(native.create_agent_app(config, "unused.exe", CATALOG)) as client:
        assert client.post("/v1/chat/completions", json=body()).status_code == 502
    checkpoint_path = next((tmp_path / "tasks").glob("*.json"))
    config.resume_task_id = checkpoint_path.stem
    with TestClient(native.create_agent_app(config, "unused.exe", CATALOG)) as client:
        result = client.post(
            "/v1/chat/completions",
            json={
                "model": "oc-second",
                "messages": [{"role": "user", "content": "Continue verification only."}],
            },
        )
        assert result.json()["choices"][0]["message"]["content"] == "RESUMED_OK"
    assert attempts == [FIRST, SECOND]


def test_quota_scope_inference_is_conservative_and_reads_nested_delay():
    explicit = native_rate_limit(
        {
            "error": {
                "status": 429,
                "message": "Model-specific rate limit reached",
                "retryAfterMs": 9000,
            }
        }
    )
    assert explicit.model_specific
    assert explicit.retry_after == 9
    unknown = native_rate_limit({"error": {"status": 429, "message": "Rate limit exceeded"}})
    assert not unknown.model_specific
    free = native_rate_limit(
        {
            "error": {
                "type": "provider.free-limit",
                "message": "model-specific usage limit reached",
            }
        }
    )
    assert free.free_limit and not free.model_specific


def test_checkpoint_paths_and_completed_turns_cannot_be_resumed(tmp_path):
    store = TaskStore(tmp_path / "tasks")
    with pytest.raises(ValueError):
        store.load("../private", tmp_path)
    task = store.start(body(), tmp_path)
    task.save("completed")
    with pytest.raises(ValueError, match="already completed"):
        store.load(task.data["id"], tmp_path)


def test_paid_fallback_is_rejected():
    with pytest.raises(ValueError):
        native.AgentSettings(fallback_models=["opencode/paid-model"])


def test_hermes_tool_handoff_keeps_native_session_across_gateway_restart(tmp_path, monkeypatch):
    prompts = []

    async def events(current, command, prompt):
        prompts.append(prompt)
        contract = json.loads(prompt.split("\n\n")[-1].split("\n")[-1])
        yield {"type": "step_start", "sessionID": "ses_tool_handoff", "part": {}}
        if len(prompts) == 1:
            yield {
                "type": "text",
                "part": {
                    "text": json.dumps(
                        {
                            "bridge_nonce": contract["bridge_nonce"],
                            "content": "List skills.",
                            "tool_calls": [{"name": "skills_list", "arguments": {}}],
                        }
                    )
                },
            }
        else:
            assert current.native_session_id == "ses_tool_handoff"
            assert "SKILLS_RESULT" in prompt
            yield {"type": "text", "part": {"text": "HANDOFF_OK"}}

    monkeypatch.setattr(native, "agent_events", events)
    config = settings(tmp_path, hermes_tools=True)
    payload = body(
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "skills_list",
                    "parameters": {"type": "object"},
                },
            }
        ]
    )
    with TestClient(native.create_agent_app(config, "unused.exe", CATALOG)) as client:
        first = client.post("/v1/chat/completions", json=payload).json()["choices"][0]
    call = first["message"]["tool_calls"][0]
    payload["messages"] += [
        first["message"],
        {"role": "tool", "tool_call_id": call["id"], "content": "SKILLS_RESULT"},
    ]
    with TestClient(native.create_agent_app(config, "unused.exe", CATALOG)) as client:
        final = client.post("/v1/chat/completions", json=payload).json()["choices"][0]
    assert final["message"]["content"] == "HANDOFF_OK"
    records = [json.loads(path.read_text()) for path in (tmp_path / "tasks").glob("*.json")]
    assert len(records) == 2
    assert sum(record["parent"] is not None for record in records) == 1


def test_streamed_checkpoint_marker_continues_same_chat_but_not_another(tmp_path, monkeypatch):
    sessions = []

    async def events(current, command, prompt):
        sessions.append(current.native_session_id)
        yield {"type": "step_start", "sessionID": "ses_chat", "part": {}}
        yield {"type": "text", "part": {"text": "ANSWER"}}

    monkeypatch.setattr(native, "agent_events", events)
    config = settings(tmp_path)
    with TestClient(native.create_agent_app(config, "unused.exe", CATALOG)) as client:
        response = client.post("/v1/chat/completions", json=body(stream=True))
        text = "".join(
            choice.get("delta", {}).get("content", "")
            for line in response.text.splitlines()
            if line.startswith("data: {")
            for choice in json.loads(line[6:]).get("choices", [])
        )
        history = body()["messages"] + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": "Continue verifying."},
        ]
        assert (
            client.post(
                "/v1/chat/completions",
                json={
                    "messages": history,
                    "model": "oc-second",
                },
            ).status_code
            == 200
        )
        unrelated = [{"role": "user", "content": "An unrelated conversation"}, *history[1:]]
        assert client.post("/v1/chat/completions", json={"messages": unrelated}).status_code == 200
    assert sessions == [None, "ses_chat", None]


@pytest.mark.asyncio
async def test_cancelling_routed_turn_closes_the_current_agent(tmp_path):
    from hermes_gateway.usage import UsageLog

    closed = []

    async def events(config, command, prompt):
        try:
            yield {"type": "step_start", "part": {}}
            yield {"type": "text", "part": {"text": "NOT_REACHED"}}
        finally:
            closed.append(config.model)

    config = settings(tmp_path)
    checkpoint = TaskStore(tmp_path / "tasks").start(body(), tmp_path)
    router = ModelRouter(
        config, CATALOG, QuotaGuard(tmp_path / "quota.json"), UsageLog(config.log_file), events
    )
    iterator = router.run("Continue the task", "unused.exe", checkpoint)
    assert (await anext(iterator))["type"] == "bridge_task"
    assert (await anext(iterator))["type"] == "bridge_model"
    assert (await anext(iterator))["type"] == "step_start"
    await iterator.aclose()
    assert closed == [FIRST]
    record = json.loads(config.log_file.read_text())
    assert record["status"] == "interrupted"
