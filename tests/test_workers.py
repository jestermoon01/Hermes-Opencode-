"""Verify real concurrency, isolation, lifecycle, MCP transport and Hermes tool handoff."""

import asyncio
import io
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from hermes_gateway import opencode_agent as native
from hermes_gateway.opencode_launcher import configure_worker_tools
from hermes_gateway.opencode_quota import OpenCodeRateLimit, QuotaGuard
from hermes_gateway.tool_relay import ToolRelay
from hermes_gateway.usage import UsageLog
from hermes_gateway.worker_mcp import serve
from hermes_gateway.workers import PREFIX, WORKER_TOOLS, WorkerManager, worker_token

FIRST = "opencode/deepseek-v4.1-flash-free"
SECOND = "opencode/glm-5.3-flash-free"
CATALOG = {"oc-first": FIRST, "oc-second": SECOND}


def settings(tmp_path, **extra):
    return native.AgentSettings(
        workdir=tmp_path,
        log_file=tmp_path / ".runtime/usage.jsonl",
        parallel_workers=True,
        **extra,
    )


def manager(config, events):
    return WorkerManager(
        config,
        "opencode.exe",
        CATALOG,
        QuotaGuard(config.log_file.parent / "quota.json"),
        UsageLog(config.log_file),
        events,
        native.AgentTurn,
    )


async def completed(pool, ids):
    result = await pool.dispatch("wait_for_workers", {"worker_ids": ids, "timeout_seconds": 3})
    assert all(record["status"] not in {"running", "queued"} for record in result)
    return result


@pytest.mark.asyncio
async def test_parallel_workers_overlap_and_respect_limit_without_touching_source(tmp_path):
    (tmp_path / "source.txt").write_text("original")
    (tmp_path / ".env").write_text("PRIVATE")
    active = peak = 0
    first_pair = asyncio.Event()
    release = asyncio.Event()

    async def events(config, command, prompt):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            first_pair.set()
        try:
            assert not (config.workdir / ".env").exists()
            assert not config.hermes_tools and not config.parallel_workers
            (config.workdir / "source.txt").write_text(config.workdir.name)
            yield {"type": "step_start", "sessionID": "ses_" + config.workdir.name}
            await release.wait()
            yield {"type": "text", "part": {"text": "WORKER_OK"}}
        finally:
            active -= 1

    pool = manager(settings(tmp_path, max_parallel_workers=2), events)
    spawned = [await pool.dispatch("spawn_worker", {"task": f"Task {i}"}) for i in range(3)]
    await asyncio.wait_for(first_pair.wait(), 3)
    assert pool.active == 3
    assert sum(record["status"] == "queued" for record in pool.records.values()) == 1
    release.set()
    results = await completed(pool, [record["id"] for record in spawned])
    assert peak == 2
    assert all(record["result"] == "WORKER_OK" for record in results)
    assert len({record["workdir"] for record in results}) == 3
    assert (tmp_path / "source.txt").read_text() == "original"
    await pool.close()


@pytest.mark.asyncio
async def test_messages_follow_native_session_at_turn_boundary(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    prompts = []

    async def events(config, command, prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            yield {"type": "step_start", "sessionID": "ses_worker"}
            entered.set()
            await release.wait()
            text = "FIRST_RESULT"
        else:
            assert config.native_session_id == "ses_worker"
            assert "Check edge cases" in prompt
            text = "FOLLOW_UP_OK"
        yield {"type": "text", "part": {"text": text}}

    pool = manager(settings(tmp_path), events)
    worker = await pool.dispatch("spawn_worker", {"task": "Investigate"})
    await asyncio.wait_for(entered.wait(), 3)
    await pool.dispatch(
        "send_worker_message",
        {
            "worker_id": worker["id"],
            "message": "Check edge cases",
        },
    )
    assert len(prompts) == 1
    release.set()
    result = (await completed(pool, [worker["id"]]))[0]
    assert result["result"] == "FOLLOW_UP_OK"
    assert result["messages"][0]["delivered"]
    await pool.close()


@pytest.mark.asyncio
async def test_cancel_queue_and_running_worker_and_explicit_resume_after_restart(tmp_path):
    entered, cleaned = asyncio.Event(), []

    async def blocked(config, command, prompt):
        try:
            yield {"type": "step_start", "sessionID": "ses_interrupted"}
            entered.set()
            await asyncio.Event().wait()
        finally:
            cleaned.append(True)

    config = settings(tmp_path, max_parallel_workers=1, max_queued_workers=2)
    pool = manager(config, blocked)
    first = await pool.dispatch("spawn_worker", {"task": "Long task"})
    await asyncio.wait_for(entered.wait(), 3)
    second = await pool.dispatch("spawn_worker", {"task": "Queued task"})
    with pytest.raises(ValueError, match="queue is full"):
        await pool.dispatch("spawn_worker", {"task": "Too many"})
    await pool.dispatch("cancel_worker", {"worker_id": second["id"]})
    assert pool.get(second["id"])["status"] == "cancelled"
    await pool.close()
    assert cleaned == [True]

    async def resumed(config, command, prompt):
        assert config.native_session_id == "ses_interrupted"
        assert "do not replay" in prompt
        yield {"type": "text", "part": {"text": "RESUMED_OK"}}

    recovered = manager(config, resumed)
    assert recovered.get(first["id"])["status"] == "interrupted"
    assert recovered.active == 0
    await recovered.dispatch("resume_worker", {"worker_id": first["id"]})
    result = (await completed(recovered, [first["id"]]))[0]
    assert result["result"] == "RESUMED_OK"
    await recovered.close()


@pytest.mark.asyncio
async def test_worker_model_fallback_and_shared_quota_block(tmp_path):
    attempts = []

    async def events(config, command, prompt):
        attempts.append(config.model)
        if config.model == FIRST:
            yield {"type": "step_start", "sessionID": "ses_switch"}
            raise OpenCodeRateLimit(60, model_specific=True)
        assert config.native_session_id == "ses_switch"
        yield {"type": "text", "part": {"text": "FALLBACK_OK"}}

    config = settings(tmp_path, automatic_model_fallback=True)
    pool = manager(config, events)
    worker = await pool.dispatch("spawn_worker", {"task": "Investigate"})
    result = (await completed(pool, [worker["id"]]))[0]
    assert result["result"] == "FALLBACK_OK"
    assert attempts == [FIRST, SECOND]
    pool.quota.record(SECOND, OpenCodeRateLimit(60, free_limit=True))
    with pytest.raises(OpenCodeRateLimit):
        await pool.dispatch("spawn_worker", {"task": "Blocked", "model": "oc-second"})
    await pool.close()


@pytest.mark.asyncio
async def test_real_git_worktree_isolated_and_dirty_source_rejected(tmp_path):
    import subprocess

    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, check=True)

    git("init")
    (tmp_path / ".gitignore").write_text(".runtime/\n")
    (tmp_path / "original.txt").write_text("original")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "initial")

    async def events(config, command, prompt):
        (config.workdir / "original.txt").write_text("worker edit")
        yield {"type": "text", "part": {"text": "EDITED"}}

    pool = manager(settings(tmp_path), events)
    first = await pool.dispatch("spawn_worker", {"task": "Edit"})
    result = (await completed(pool, [first["id"]]))[0]
    assert result["workspace_kind"] == "git-worktree"
    assert (Path(result["workdir"]) / "original.txt").read_text() == "worker edit"
    assert (tmp_path / "original.txt").read_text() == "original"
    (tmp_path / "original.txt").write_text("uncommitted")
    second = await pool.dispatch("spawn_worker", {"task": "Another edit"})
    result = (await completed(pool, [second["id"]]))[0]
    assert result["status"] == "failed"
    assert "Commit or stash" in result["error"]
    await pool.close()
    git("worktree", "remove", "--force", first["workdir"] or pool.get(first["id"])["workdir"])


def test_hermes_worker_tool_round_trip_and_mcp_transport(tmp_path, monkeypatch):
    async def events(config, command, prompt):
        if config.workdir != tmp_path:
            yield {"type": "text", "part": {"text": "Worker found the answer: 42"}}
            return
        contract = json.loads(prompt.split("\n\n")[-1].split("\n")[-1])
        assert "as Hermes" in prompt
        if "WORKER_RESULT" in prompt:
            text = "I checked the result: 42."
        else:
            text = json.dumps(
                {
                    "bridge_nonce": contract["bridge_nonce"],
                    "content": "I’ll delegate the investigation.",
                    "tool_calls": [
                        {"name": PREFIX + "spawn_worker", "arguments": {"task": "Find the answer"}}
                    ],
                }
            )
        yield {"type": "text", "part": {"text": text}}

    monkeypatch.setattr(native, "agent_events", events)
    config = settings(tmp_path, hermes_tools=True)
    token = worker_token(config.log_file.parent)
    headers = {"Authorization": "Bearer " + token}
    tools = [
        {
            "type": "function",
            "function": {
                "name": PREFIX + item["name"],
                "description": item["description"],
                "parameters": item["inputSchema"],
            },
        }
        for item in WORKER_TOOLS
    ]
    payload = {"messages": [{"role": "user", "content": "Delegate investigation"}], "tools": tools}
    with TestClient(native.create_agent_app(config, "unused.exe", CATALOG)) as client:
        assert client.get("/workers/tools").status_code == 403
        assert len(client.get("/workers/tools", headers=headers).json()) == 7
        first = client.post("/v1/chat/completions", json=payload).json()["choices"][0]
        assert first["finish_reason"] == "tool_calls"
        call = first["message"]["tool_calls"][0]
        worker = client.post(
            "/workers/call",
            headers=headers,
            json={
                "name": "spawn_worker",
                "arguments": json.loads(call["function"]["arguments"]),
            },
        ).json()
        result = client.post(
            "/workers/call",
            headers=headers,
            json={
                "name": "wait_for_workers",
                "arguments": {"worker_ids": [worker["id"]]},
            },
        ).json()[0]
        assert result["status"] == "completed"
        payload["messages"] += [
            first["message"],
            {
                "role": "tool",
                "tool_call_id": call["id"],
                "content": "WORKER_RESULT: " + result["result"],
            },
        ]
        payload["stream"] = True
        response = client.post("/v1/chat/completions", json=payload)
        assert "I checked the result: 42." in response.text
        assert "[OpenCode]" not in response.text
        assert "bridge_nonce" not in response.text

        def transport(request):
            reply = client.request(
                request.method, request.url.path, content=request.content, headers=headers
            )
            return httpx.Response(reply.status_code, json=reply.json())

        rpc_input = io.StringIO(
            "\n".join(
                json.dumps(message)
                for message in [
                    {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
                    {"method": "notifications/initialized"},
                    {"id": 2, "method": "tools/list"},
                    {
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "get_worker_status",
                            "arguments": {"worker_id": worker["id"]},
                        },
                    },
                ]
            )
        )
        output = io.StringIO()
        with httpx.Client(
            base_url="http://testserver", transport=httpx.MockTransport(transport)
        ) as proxy:
            serve(rpc_input, output, proxy)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        assert len(replies) == 3
        assert replies[0]["result"]["protocolVersion"] == "2025-11-25"
        assert len(replies[1]["result"]["tools"]) == 7
        assert not replies[2]["result"]["isError"]


def test_worker_tools_only_allowed_when_enabled_and_configured(tmp_path):
    item = WORKER_TOOLS[0]
    payload = {
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": PREFIX + item["name"],
                    "parameters": item["inputSchema"],
                },
            }
        ]
    }
    with pytest.raises(ValueError):
        ToolRelay(payload)
    assert PREFIX + item["name"] in ToolRelay(payload, allow_workers=True).functions
    config = {"agent": {"max_turns": 12}}
    toolsets = configure_worker_tools(
        config, settings(tmp_path, hermes_tools=True), "http://127.0.0.1:8787"
    )
    assert toolsets == ["memory", "skills", "mcp-opencode_workers"]
    assert "opencode_workers" in config["mcp_servers"]
    assert config["agent"]["max_turns"] == 100


def test_default_greeting_contains_only_answer_and_keeps_continuity(tmp_path, monkeypatch):
    sessions = []

    async def events(config, command, prompt):
        sessions.append(config.native_session_id)
        yield {"type": "step_start", "sessionID": "ses_quiet"}
        yield {"type": "text", "part": {"text": "Hey! What can I help with?"}}

    monkeypatch.setattr(native, "agent_events", events)
    config = settings(tmp_path, task_continuity=True, hermes_tools=True)
    with TestClient(native.create_agent_app(config, "unused.exe", CATALOG)) as client:
        messages = [{"role": "user", "content": "hey"}]
        response = client.post("/v1/chat/completions", json={"messages": messages, "stream": True})
        content = "".join(
            choice.get("delta", {}).get("content", "")
            for line in response.text.splitlines()
            if line.startswith("data: {")
            for choice in json.loads(line[6:]).get("choices", [])
        )
        assert content.strip() == "Hey! What can I help with?"
        messages += [
            {"role": "assistant", "content": content},
            {"role": "user", "content": "Continue"},
        ]
        assert client.post("/v1/chat/completions", json={"messages": messages}).status_code == 200
    assert sessions == [None, "ses_quiet"]


@pytest.mark.asyncio
async def test_worker_timeout_reports_reason_and_does_not_retry(tmp_path):
    attempts = []

    async def timed_out(config, command, prompt):
        attempts.append(config.model)
        yield {"type": "step_start", "sessionID": "ses_timeout"}
        raise TimeoutError

    pool = manager(settings(tmp_path), timed_out)
    worker = await pool.dispatch("spawn_worker", {"task": "Work"})
    result = (await completed(pool, [worker["id"]]))[0]
    assert result["status"] == "timed_out"
    assert "600s turn timeout" in result["error"]
    assert attempts == [FIRST]
    await pool.close()


def test_authenticated_reconnection_and_safe_shutdown(tmp_path, monkeypatch):
    from hermes_gateway import opencode_launcher

    async def events(config, command, prompt):
        yield {"type": "step_start"}
        await asyncio.Event().wait()

    monkeypatch.setattr(native, "agent_events", events)
    config = settings(tmp_path)
    app = native.create_agent_app(config, "unused.exe", CATALOG)
    stopped = []
    app.state.shutdown = lambda: stopped.append(True)
    headers = {"Authorization": "Bearer " + worker_token(config.log_file.parent)}
    client_class = httpx.Client
    with TestClient(app) as client:

        def transport(request):
            reply = client.request(request.method, request.url.path, headers=dict(request.headers))
            return httpx.Response(reply.status_code, json=reply.json())

        monkeypatch.setattr(
            opencode_launcher.httpx,
            "Client",
            lambda **kwargs: client_class(
                transport=httpx.MockTransport(transport),
                **kwargs,
            ),
        )
        state = opencode_launcher.worker_state("http://testserver", config)
        assert state["active"] == 0
        changed_config = config.model_copy(update={"max_parallel_workers": 1})
        assert opencode_launcher.worker_state("http://testserver", changed_config) is None
        assert (
            opencode_launcher.worker_state(
                "http://testserver",
                changed_config,
                require_settings=False,
            )
            is not None
        )
        assert client.post("/workers/shutdown").status_code == 403
        worker = client.post(
            "/workers/call",
            headers=headers,
            json={
                "name": "spawn_worker",
                "arguments": {"task": "Run"},
            },
        ).json()
        assert client.post("/workers/shutdown", headers=headers).status_code == 409
        client.post(
            "/workers/call",
            headers=headers,
            json={
                "name": "cancel_worker",
                "arguments": {"worker_id": worker["id"]},
            },
        ).raise_for_status()
        assert client.post("/workers/shutdown", headers=headers).json()["status"] == "stopping"
        # Run a request long enough for the scheduled shutdown callback to execute.
        client.portal.call(asyncio.sleep, 0.15)
        assert stopped == [True]


@pytest.mark.asyncio
async def test_crash_resume_refuses_to_overlap_a_surviving_native_process(tmp_path, monkeypatch):
    from hermes_gateway import workers

    async def events(config, command, prompt):
        yield {"type": "text", "part": {"text": "DONE"}}

    config = settings(tmp_path)
    pool = manager(config, events)
    worker = await pool.dispatch("spawn_worker", {"task": "Work"})
    await completed(pool, [worker["id"]])
    record = pool.get(worker["id"])
    record["status"] = "interrupted"
    record["process"] = {"pid": 12345, "identity": "birth-time"}
    pool.save(record)
    monkeypatch.setattr(workers, "process_identity", lambda pid: "birth-time")
    with pytest.raises(ValueError, match="still running"):
        await pool.dispatch("resume_worker", {"worker_id": worker["id"]})
    monkeypatch.setattr(workers, "process_identity", lambda pid: "different-process")
    await pool.dispatch("resume_worker", {"worker_id": worker["id"]})
    assert (await completed(pool, [worker["id"]]))[0]["status"] == "completed"
    await pool.close()


def test_process_identity_is_stable_for_a_running_process():
    import os

    from hermes_gateway.processes import process_identity

    identity = process_identity(os.getpid())
    assert identity not in {None, "unverified"}
    assert process_identity(os.getpid()) == identity


def test_existing_hermes_gateway_check_reads_process_identity(tmp_path):
    import os

    from hermes_gateway.launcher import existing_home_gateway

    (tmp_path / "gateway.pid").write_text(json.dumps({"pid": os.getpid()}))
    assert existing_home_gateway(tmp_path)
