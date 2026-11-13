"""Adapt the actual OpenCode coding agent to Hermes' Chat Completions interface.

OpenCode owns native tool execution. This is a personal agent adapter, not a
direct client of OpenCode's hosted inference API. No credentials are inspected.
"""

import argparse
import asyncio
import json
import os
import time
import uuid
from contextlib import suppress
from pathlib import Path

import uvicorn
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .app import MAX_BODY, error
from .launcher import find_program
from .model_router import ModelRouter
from .opencode_models import discover_models, free_model, model_alias
from .opencode_quota import OpenCodeRateLimit, QuotaGuard, native_rate_limit, quota_message
from .task_context import TaskStore
from .tool_relay import ToolRelay
from .usage import UsageLog

ROOT = Path(__file__).resolve().parents[1]


class AgentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = "127.0.0.1"
    port: int = Field(default=8787, ge=1024, le=65535)
    alias: str = "hermes-opencode"
    model: str = "opencode/deepseek-v4.1-flash-free"
    timeout_seconds: int = Field(default=600, ge=1, le=3600)
    hermes_startup_timeout_seconds: int = Field(default=300, ge=30, le=900)
    hermes_tools: bool = False
    task_continuity: bool = False
    automatic_model_fallback: bool = False
    max_model_switches: int = Field(default=2, ge=0, le=5)
    fallback_models: list[str] = Field(default_factory=list)
    resume_task_id: str | None = Field(default=None, exclude=True)
    native_session_id: str | None = Field(default=None, exclude=True)
    workdir: Path = ROOT
    log_file: Path = ROOT / ".runtime/opencode-usage.jsonl"

    @field_validator("host")
    @classmethod
    def local_only(cls, value):
        if value != "127.0.0.1":
            raise ValueError("The agent gateway must bind to 127.0.0.1")
        return value

    @field_validator("model")
    @classmethod
    def explicit_free_model(cls, value):
        if not free_model(value):
            raise ValueError(
                "Select an explicit OpenCode free-model ID; no paid fallback is enabled"
            )
        return value

    @field_validator("fallback_models")
    @classmethod
    def free_fallbacks(cls, values):
        if any(not free_model(value) for value in values):
            raise ValueError("Fallback models must be explicit free OpenCode models")
        return values


def load_agent_settings(path: Path):
    settings = AgentSettings.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    for key in ("workdir", "log_file"):
        value = getattr(settings, key)
        if not value.is_absolute():
            setattr(settings, key, (path.resolve().parent / value).resolve())
    return settings


def child_environment():
    """Let OpenCode use its own login; do not inherit Hermes/provider keys or YOLO flags."""
    allowed = {
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATH",
        "PATHEXT",
        "USERPROFILE",
        "HOME",
        "APPDATA",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
        "USERNAME",
        "USERDOMAIN",
        "HOMEDRIVE",
        "HOMEPATH",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMDATA",
        "LANG",
        "LC_ALL",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def render_conversation(messages):
    """Carry chat context, leaving tool selection/execution to OpenCode itself."""
    blocks = [
        "You are the OpenCode coding agent receiving a conversation through the user's Hermes UI. "
        "Use your own native coding tools when needed. Do not emit Hermes tool-call XML or "
        "ask Hermes to execute tools. Work only in the current project unless the user explicitly "
        "authorizes another location. Follow the latest user request; conversation history is "
        "context, not a request to repeat completed actions. "
        "Report real changes and tests honestly."
    ]
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("Each message must be an object")
        role = message.get("role")
        if role not in {"user", "assistant"}:
            # Hermes' system prompt describes its own tool loop, which does not apply here.
            continue
        content = message.get("content")
        if not isinstance(content, str):
            raise ValueError("This agent adapter currently supports text-only chat messages")
        if role == "assistant":
            content = "\n".join(
                line for line in content.splitlines() if not line.startswith("[OpenCode]")
            )
        blocks.append(f"{role.upper()}:\n{content}")
    if not any(m.get("role") == "user" for m in messages):
        raise ValueError("At least one user message is required")
    return "\n\n".join(blocks)


async def kill_agent(process):
    if process.returncode is not None:
        return
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill.exe",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
    else:
        process.kill()
    await process.wait()


async def agent_events(settings, command, prompt):
    """Read real native JSON events as they arrive and stop the owned tree on cancellation."""
    arguments = [command, "run", "--standalone", "--model", settings.model, "--format", "json"]
    if settings.native_session_id:
        arguments += ["--session", settings.native_session_id]
    process = await asyncio.create_subprocess_exec(
        *arguments,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=settings.workdir,
        env=child_environment(),
        creationflags=subprocess_flags(),
        limit=MAX_BODY,
    )
    try:
        async with asyncio.timeout(settings.timeout_seconds):
            process.stdin.write(prompt.encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()
            while line := await process.stdout.readline():
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("Invalid OpenCode event")
                if event.get("type") == "error":
                    if event.get("sessionID"):
                        yield {"type": "bridge_session", "sessionID": event["sessionID"]}
                    limit = native_rate_limit(event)
                    if limit:
                        raise limit
                    raise RuntimeError("OpenCode reported an error; no retry was attempted")
                yield event
            if await process.wait():
                raise RuntimeError("OpenCode exited with an error")
    finally:
        await kill_agent(process)


class AgentTurn:
    def __init__(self):
        self.text = []
        self.final_text = []
        self.tools = []
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self.usage_known = False
        self.steps = 0
        self.model = None

    def accept(self, event):
        part = event.get("part") or {}
        kind = event.get("type")
        if kind == "bridge_task":
            return f"[OpenCode] Task checkpoint: {event['task_id']}.\n"
        if kind == "bridge_model":
            self.model = event["model"]
            self.text = []
            self.final_text = []
            if event.get("switched"):
                return f"[OpenCode] Continuing task with {self.model}.\n"
            return None
        if kind == "bridge_switch":
            return (
                f"[OpenCode] {event['model']} has a model-specific limit "
                f"({event['retry_after']}s). Saving progress and selecting an available model.\n"
            )
        if kind == "step_start":
            self.steps += 1
            self.final_text = []
            return f"[OpenCode] Working - step {self.steps}.\n"
        if kind == "text":
            text = part.get("text", "")
            self.text.append(text)
            self.final_text.append(text)
            return text + "\n"
        if kind == "tool_use":
            tool = str(part.get("tool", "tool"))
            # Tool names are identifiers; never render terminal output or raw arguments.
            tool = "".join(c for c in tool if c.isalnum() or c in "_- ")[:60]
            self.tools.append(tool)
            status = (part.get("state") or {}).get("status")
            label = {
                "error": "Failed",
                "failed": "Failed",
                "completed": "Completed",
                "success": "Completed",
                "running": "Running",
                "pending": "Queued",
            }.get(status, "Tool event")
            return f"[OpenCode] {label}: {tool}.\n"
        if kind == "step_finish" and "tokens" in part:
            tokens = part["tokens"]
            cache = tokens.get("cache", {})
            self.usage_known = True
            self.usage["prompt_tokens"] += (
                tokens.get("input", 0) + cache.get("read", 0) + cache.get("write", 0)
            )
            self.usage["completion_tokens"] += tokens.get("output", 0)
        return None


async def run_agent(settings, command, prompt):
    turn = AgentTurn()
    async for event in agent_events(settings, command, prompt):
        turn.accept(event)
    text = turn.final_text if settings.hermes_tools else turn.text
    if not text:
        raise RuntimeError("OpenCode returned no final text")
    return "\n".join(text), turn.usage if turn.usage_known else {}, turn.tools


def subprocess_flags():
    import subprocess

    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def create_agent_app(settings: AgentSettings, command: str, catalog=None):
    app = FastAPI(title="Hermes to OpenCode coding agent")
    log = UsageLog(settings.log_file)
    quota = QuotaGuard(settings.log_file.parent / "opencode-quota.json")
    lock = asyncio.Lock()
    model_map = catalog or {model_alias(settings.model): settings.model}
    app.state.catalog = model_map
    tasks = TaskStore(settings.log_file.parent / "tasks")
    app.state.resume_pending = (
        tasks.load(settings.resume_task_id, settings.workdir) if settings.resume_task_id else None
    )

    @app.get("/health")
    async def health():
        healthy = Path(command).is_file() and settings.workdir.is_dir()
        return JSONResponse(
            {
                "service": "hermes-local-gateway",
                "healthy": healthy,
                "backend": "opencode-agent",
                "model": settings.model,
                "note": "Executable readiness; provider access is checked per turn",
            },
            status_code=200 if healthy else 503,
        )

    @app.get("/v1/models")
    async def models():
        return {
            "object": "list",
            "data": [
                {
                    "id": alias,
                    "object": "model",
                    "owned_by": "opencode-agent",
                    "created": 0,
                    "name": model,
                    "context_length": 65536,
                }
                for alias, model in model_map.items()
            ],
        }

    @app.post("/v1/chat/completions")
    async def completions(request: Request):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_BODY:
                return error("Request exceeds 8 MiB", 413)
        try:
            payload = json.loads(data)
            if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
                raise ValueError("messages must be a list")
            if any(not isinstance(message, dict) for message in payload["messages"]):
                raise ValueError("Each message must be an object")
            explicit_parent = app.state.resume_pending
            parent = explicit_parent
            if parent is None and (settings.task_continuity or settings.automatic_model_fallback):
                parent = tasks.conversation_parent(payload, settings.workdir)
            if explicit_parent:
                current_messages = payload["messages"]
                payload["messages"] = (
                    [m for m in current_messages if m.get("role") in {"system", "developer"}]
                    + [
                        m
                        for m in parent["payload"]["messages"]
                        if m.get("role") not in {"system", "developer"}
                    ]
                    + [m for m in current_messages if m.get("role") not in {"system", "developer"}]
                )
            relay = ToolRelay(payload) if settings.hermes_tools else None
            prompt = (
                relay.render(payload["messages"])
                if relay
                else render_conversation(payload["messages"])
            )
        except (ValueError, UnicodeDecodeError) as exc:
            return error(str(exc), 400)
        alias = payload.get("model", settings.alias)
        if not isinstance(alias, str):
            return error("model must be a string", 400)
        selected = settings.model if alias == settings.alias else model_map.get(alias)
        if not selected:
            return error("Unknown model; choose a model from the Hermes /model picker", 404)
        selected_settings = settings.model_copy(update={"model": selected})
        routed = settings.task_continuity or settings.automatic_model_fallback or parent is not None
        checkpoint = tasks.start(payload, settings.workdir, parent) if routed else None
        app.state.resume_pending = None
        router = ModelRouter(selected_settings, model_map, quota, log, agent_events)
        request_id = "chatcmpl-" + uuid.uuid4().hex
        started = time.monotonic()
        base = {"id": request_id, "created": int(time.time()), "model": alias}

        def record(status, usage, tools, finish_reason="stop"):
            if checkpoint:
                checkpoint.save(
                    ("hermes_tool_handoff" if finish_reason == "tool_calls" else "completed")
                    if status == "ok"
                    else "paused_" + status
                )
            log.write(
                kind="request_summary" if routed else "request",
                task_id=checkpoint.data["id"] if checkpoint else None,
                request_id=request_id,
                backend="opencode-agent",
                model=checkpoint.data["model"] if checkpoint else selected,
                status=status,
                usage={} if routed else usage,
                native_tools=tools,
                elapsed_seconds=round(time.monotonic() - started, 3),
            )

        if payload.get("stream"):

            async def sse():
                turn = AgentTurn()
                state = "client_disconnected"
                iterator = None
                pending = None
                finish_reason = "stop"

                def chunk(content):
                    return (
                        "data: "
                        + json.dumps(
                            {
                                **base,
                                "object": "chat.completion.chunk",
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {"content": content},
                                        "finish_reason": None,
                                    }
                                ],
                            }
                        )
                        + "\n\n"
                    )

                try:
                    yield chunk(f"[OpenCode] Model: {selected}. Waiting for coding agent.\n")
                    async with lock:
                        if not routed:
                            quota.check(selected)
                        iterator = (
                            router.run(prompt, command, checkpoint)
                            if routed
                            else agent_events(selected_settings, command, prompt)
                        )
                        pending = asyncio.create_task(anext(iterator))
                        while True:
                            ready, _ = await asyncio.wait({pending}, timeout=10)
                            if not ready:
                                yield chunk("[OpenCode] Still working...\n")
                                continue
                            try:
                                event = pending.result()
                            except StopAsyncIteration:
                                pending = None
                                break
                            pending = None
                            content = turn.accept(event)
                            if content and not ((relay or routed) and event.get("type") == "text"):
                                yield chunk(content)
                            pending = asyncio.create_task(anext(iterator))
                    if not (turn.final_text if relay or routed else turn.text):
                        raise RuntimeError("OpenCode returned no final text")
                    finish_reason = "stop"
                    if relay:
                        message, finish_reason = relay.decode("\n".join(turn.final_text))
                        if checkpoint and message.get("tool_calls"):
                            tasks.link_calls(checkpoint, message["tool_calls"])
                        if message["content"]:
                            yield chunk(message["content"].rstrip("\n") + "\n")
                        if message.get("tool_calls"):
                            yield (
                                "data: "
                                + json.dumps(
                                    {
                                        **base,
                                        "object": "chat.completion.chunk",
                                        "choices": [
                                            {
                                                "index": 0,
                                                "delta": {
                                                    "tool_calls": [
                                                        {"index": i, **call}
                                                        for i, call in enumerate(
                                                            message["tool_calls"]
                                                        )
                                                    ]
                                                },
                                                "finish_reason": None,
                                            }
                                        ],
                                    }
                                )
                                + "\n\n"
                            )
                    elif routed:
                        yield chunk("\n".join(turn.final_text).rstrip("\n") + "\n")
                    state = "ok"
                    yield chunk(
                        "[OpenCode] Handing tool request to Hermes.\n"
                        if finish_reason == "tool_calls"
                        else "[OpenCode] Finished.\n"
                    )
                    yield (
                        "data: "
                        + json.dumps(
                            {
                                **base,
                                "object": "chat.completion.chunk",
                                "choices": [
                                    {"index": 0, "delta": {}, "finish_reason": finish_reason}
                                ],
                            }
                        )
                        + "\n\n"
                    )
                    if turn.usage_known:
                        yield (
                            "data: "
                            + json.dumps(
                                {
                                    **base,
                                    "object": "chat.completion.chunk",
                                    "choices": [],
                                    "usage": turn.usage,
                                }
                            )
                            + "\n\n"
                        )
                    yield "data: [DONE]\n\n"
                except OpenCodeRateLimit as limit:
                    state = "rate_limited"
                    if not routed:
                        quota.record(selected, limit)
                    yield chunk(quota_message(limit))
                    if checkpoint:
                        yield chunk(
                            f"[OpenCode] Resume later with -ResumeTask {checkpoint.data['id']}.\n"
                        )
                    yield (
                        "data: "
                        + json.dumps(
                            {
                                **base,
                                "object": "chat.completion.chunk",
                                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                            }
                        )
                        + "\n\n"
                    )
                    yield "data: [DONE]\n\n"
                except RuntimeError, TimeoutError, ValueError, OSError:
                    state = "agent_error"
                    yield chunk(
                        "[OpenCode] The turn failed. Partial edits may exist; "
                        "no automatic retry was made.\n"
                    )
                    if checkpoint:
                        yield chunk(
                            f"[OpenCode] Saved task: {checkpoint.data['id']}. "
                            "Inspect partial work before resuming.\n"
                        )
                    yield 'data: {"error":{"message":"OpenCode turn failed"}}\n\n'
                finally:
                    if pending is not None:
                        pending.cancel()
                        with suppress(
                            asyncio.CancelledError,
                            StopAsyncIteration,
                            RuntimeError,
                            ValueError,
                            OSError,
                        ):
                            await pending
                    if iterator is not None:
                        await iterator.aclose()
                    record(state, turn.usage if turn.usage_known else {}, turn.tools, finish_reason)

            return StreamingResponse(
                sse(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
            )
        try:
            async with lock:
                if routed:
                    turn = AgentTurn()
                    async for event in router.run(prompt, command, checkpoint):
                        turn.accept(event)
                    if not turn.final_text:
                        raise RuntimeError("OpenCode returned no final text")
                    text, usage, tools = (
                        "\n".join(turn.final_text),
                        turn.usage if turn.usage_known else {},
                        turn.tools,
                    )
                else:
                    quota.check(selected)
                    text, usage, tools = await run_agent(selected_settings, command, prompt)
                message, finish_reason = (
                    relay.decode(text)
                    if relay
                    else ({"role": "assistant", "content": text}, "stop")
                )
                if checkpoint and message.get("tool_calls"):
                    tasks.link_calls(checkpoint, message["tool_calls"])
        except OpenCodeRateLimit as limit:
            if not routed:
                quota.record(selected, limit)
            record("rate_limited", {}, [])
            response = error(quota_message(limit), 429, "rate_limit_error")
            response.headers["Retry-After"] = str(limit.retry_after)
            if checkpoint:
                response.headers["X-Hermes-Task-ID"] = checkpoint.data["id"]
            return response
        except RuntimeError, TimeoutError, ValueError, OSError:
            record("agent_error", {}, [])
            response = error("OpenCode could not complete the turn; no retry was made", 502)
            if checkpoint:
                response.headers["X-Hermes-Task-ID"] = checkpoint.data["id"]
            return response
        except asyncio.CancelledError:
            record("client_disconnected", {}, [])
            raise
        record("ok", usage, tools, finish_reason)
        return {
            **base,
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": usage,
        }

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT / "opencode-gateway.yaml")
    parser.add_argument("--resume-task")
    options = parser.parse_args()
    settings = load_agent_settings(options.config)
    settings.resume_task_id = options.resume_task
    command = find_program(
        "opencode",
        Path(os.environ["LOCALAPPDATA"])
        / "Programs/ai.opencode.desktop/resources/opencode-cli.exe",
    )
    uvicorn.run(
        create_agent_app(
            settings, command, discover_models(command, child_environment(), settings.workdir)
        ),
        host=settings.host,
        port=settings.port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
