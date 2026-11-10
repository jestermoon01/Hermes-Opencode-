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
from .opencode_models import discover_models, free_model, model_alias
from .opencode_quota import OpenCodeRateLimit, QuotaGuard, native_rate_limit, quota_message
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
    process = await asyncio.create_subprocess_exec(
        command,
        "run",
        "--standalone",
        "--model",
        settings.model,
        "--format",
        "json",
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
        self.tools = []
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self.usage_known = False
        self.steps = 0

    def accept(self, event):
        part = event.get("part") or {}
        kind = event.get("type")
        if kind == "step_start":
            self.steps += 1
            return f"[OpenCode] Working - step {self.steps}.\n"
        if kind == "text":
            text = part.get("text", "")
            self.text.append(text)
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
    if not turn.text:
        raise RuntimeError("OpenCode returned no final text")
    return "\n".join(turn.text), turn.usage if turn.usage_known else {}, turn.tools


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
            prompt = render_conversation(payload["messages"])
        except (ValueError, UnicodeDecodeError) as exc:
            return error(str(exc), 400)
        alias = payload.get("model", settings.alias)
        if not isinstance(alias, str):
            return error("model must be a string", 400)
        selected = settings.model if alias == settings.alias else model_map.get(alias)
        if not selected:
            return error("Unknown model; choose a model from the Hermes /model picker", 404)
        selected_settings = settings.model_copy(update={"model": selected})
        request_id = "chatcmpl-" + uuid.uuid4().hex
        started = time.monotonic()
        base = {"id": request_id, "created": int(time.time()), "model": alias}

        def record(status, usage, tools):
            log.write(
                request_id=request_id,
                backend="opencode-agent",
                model=selected,
                status=status,
                usage=usage,
                native_tools=tools,
                elapsed_seconds=round(time.monotonic() - started, 3),
            )

        if payload.get("stream"):

            async def sse():
                turn = AgentTurn()
                state = "client_disconnected"
                iterator = None
                pending = None

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
                        quota.check(selected)
                        iterator = agent_events(selected_settings, command, prompt)
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
                            if content:
                                yield chunk(content)
                            pending = asyncio.create_task(anext(iterator))
                    if not turn.text:
                        raise RuntimeError("OpenCode returned no final text")
                    state = "ok"
                    yield chunk("[OpenCode] Finished.\n")
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
                    quota.record(selected, limit)
                    yield chunk(quota_message(limit))
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
                    record(state, turn.usage if turn.usage_known else {}, turn.tools)

            return StreamingResponse(
                sse(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
            )
        try:
            async with lock:
                quota.check(selected)
                text, usage, tools = await run_agent(selected_settings, command, prompt)
        except OpenCodeRateLimit as limit:
            quota.record(selected, limit)
            record("rate_limited", {}, [])
            response = error(quota_message(limit), 429, "rate_limit_error")
            response.headers["Retry-After"] = str(limit.retry_after)
            return response
        except RuntimeError, TimeoutError, ValueError, OSError:
            record("agent_error", {}, [])
            return error("OpenCode could not complete the turn; no retry was made", 502)
        record("ok", usage, tools)
        return {
            **base,
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": usage,
        }

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT / "opencode-gateway.yaml")
    options = parser.parse_args()
    settings = load_agent_settings(options.config)
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
