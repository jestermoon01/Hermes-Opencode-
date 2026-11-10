"""Priority routing with transparent JSON/SSE and failover before response delivery."""

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .config import Backend, Settings
from .usage import StreamUsage, UsageLog

MAX_BODY = 8 * 1024 * 1024


def error(message: str, status: int, kind: str = "gateway_error") -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": kind}}, status_code=status)


def backend_headers(backend: Backend) -> dict[str, str]:
    key = os.environ.get(backend.api_key_env, "") if backend.api_key_env else ""
    return {"Authorization": f"Bearer {key}"} if key else {}


def create_app(settings: Settings, transport=None) -> FastAPI:
    log = UsageLog(settings.log_file)
    cooldowns: dict[str, float] = {}
    backends = sorted((b for b in settings.backends if b.enabled), key=lambda b: b.priority)
    timeout = httpx.Timeout(settings.read_timeout, connect=settings.connect_timeout)

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(
            timeout=timeout, transport=transport, trust_env=False, follow_redirects=False
        ) as client:
            app.state.client = client
            yield

    app = FastAPI(title="Hermes Local Gateway", lifespan=lifespan)

    async def healthy(backend):
        if cooldowns.get(backend.name, 0) > time.monotonic():
            return False
        try:
            result = await app.state.client.get(
                backend.health_url, headers=backend_headers(backend), timeout=3
            )
            return result.status_code == 200
        except httpx.HTTPError:
            return False

    @app.get("/health")
    async def health():
        states = await asyncio.gather(*(healthy(b) for b in backends))
        return JSONResponse(
            {
                "service": "hermes-local-gateway",
                "healthy": any(states),
                "backends": dict(zip((b.name for b in backends), states, strict=True)),
            },
            status_code=200 if any(states) else 503,
        )

    @app.get("/v1/models")
    async def models():
        aliases = sorted({alias for b in backends for alias in b.models})
        return {
            "object": "list",
            "data": [
                {"id": alias, "object": "model", "created": 0, "owned_by": "local-gateway"}
                for alias in aliases
            ],
        }

    @app.post("/v1/chat/completions")
    async def completions(request: Request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_BODY:
                return error("Request exceeds 8 MiB", 413)
        try:
            payload = json.loads(raw)
        except ValueError, UnicodeDecodeError:
            return error("Expected a JSON request body", 400)
        if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
            return error("messages must be a list", 400)
        alias = payload.get("model", settings.model)
        if not isinstance(alias, str) or not isinstance(payload.get("stream", False), bool):
            return error("model must be a string and stream must be a boolean", 400)
        if payload.get("stream_options") is not None and not isinstance(
            payload["stream_options"], dict
        ):
            return error("stream_options must be an object", 400)
        candidates = [b for b in backends if alias in b.models]
        if not candidates:
            return error("Unknown model alias; see /v1/models", 404)
        request_id = uuid.uuid4().hex
        output_limit = payload.get("max_completion_tokens", payload.get("max_tokens", 0)) or 0
        if not isinstance(output_limit, int) or output_limit < 0:
            return error("Output token limit must be a nonnegative integer", 400)
        for backend in candidates:
            fields = {
                "request_id": request_id,
                "backend": backend.name,
                "model": alias,
                "upstream_model": backend.models[alias],
                "stream": bool(payload.get("stream")),
            }
            if output_limit > backend.context_limit:
                log.write(**fields, status="context_limit_exceeded")
                continue
            if not await healthy(backend):
                log.write(**fields, status="unhealthy_or_cooling_down")
                continue
            body = {**payload, "model": backend.models[alias]}
            if body.get("stream"):
                body["stream_options"] = {
                    **(body.get("stream_options") or {}),
                    "include_usage": True,
                }
            upstream = None
            started = time.monotonic()
            try:
                upstream = await app.state.client.send(
                    app.state.client.build_request(
                        "POST",
                        backend.base_url + "/chat/completions",
                        json=body,
                        headers=backend_headers(backend),
                    ),
                    stream=True,
                )
                if upstream.status_code >= 400:
                    if upstream.status_code == 429:
                        try:
                            wait = max(60, float(upstream.headers.get("retry-after", "60")))
                        except ValueError:
                            try:
                                retry_time = parsedate_to_datetime(upstream.headers["retry-after"])
                                wait = max(60, (retry_time - datetime.now(UTC)).total_seconds())
                            except ValueError, TypeError, KeyError:
                                wait = 60
                        cooldowns[backend.name] = time.monotonic() + wait
                    log.write(**fields, status="upstream_error", http_status=upstream.status_code)
                    await upstream.aclose()
                    continue
                if upstream.status_code != 200:
                    await upstream.aclose()
                    log.write(**fields, status="unexpected_http_status")
                    continue
                if body.get("stream"):
                    if "text/event-stream" not in upstream.headers.get("content-type", ""):
                        await upstream.aclose()
                        log.write(**fields, status="invalid_stream_content_type")
                        continue
                    iterator = upstream.aiter_bytes()
                    # Wait for an actual SSE data line before committing downstream headers.
                    first = bytearray()
                    async with asyncio.timeout(settings.read_timeout):
                        while b"data:" not in first or b"\n" not in first.split(b"data:", 1)[1]:
                            first.extend(await anext(iterator))
                            if len(first) > 1024 * 1024:
                                raise ValueError("SSE prelude exceeded 1 MiB")
                    first_line = bytes(first).split(b"data:", 1)[1].split(b"\n", 1)[0].strip()
                    if first_line != b"[DONE]" and "error" in json.loads(first_line):
                        raise ValueError("Upstream returned an SSE error")

                    async def relay(
                        response=upstream,
                        chunks=iterator,
                        initial=bytes(first),
                        metadata=fields,
                        begin=started,
                    ):
                        usage = StreamUsage()
                        status = "client_disconnected"
                        try:
                            usage.feed(initial)
                            yield initial
                            async for data in chunks:
                                usage.feed(data)
                                yield data
                            status = "ok" if usage.done else "incomplete_stream"
                            if not usage.done:
                                yield (
                                    b'data: {"error":{"message":"Upstream stream ended early"}}\n\n'
                                )
                        except httpx.HTTPError, ValueError:
                            status = "stream_error"
                            yield b'data: {"error":{"message":"Upstream stream failed"}}\n\n'
                        finally:
                            await response.aclose()
                            log.write(
                                **metadata,
                                status=status,
                                usage=usage.usage,
                                tool_calls=usage.tool_calls,
                                elapsed_seconds=round(time.monotonic() - begin, 3),
                            )

                    return StreamingResponse(
                        relay(),
                        media_type="text/event-stream",
                        headers={
                            "Cache-Control": "no-cache",
                            "X-Gateway-Backend": backend.name,
                            "X-Request-ID": request_id,
                        },
                    )
                data = await upstream.aread()
                document = json.loads(data)
                if "error" in document or "choices" not in document:
                    raise ValueError("Invalid upstream completion")
                calls = sum(
                    len(c.get("message", {}).get("tool_calls", []))
                    for c in document.get("choices", [])
                )
                log.write(
                    **fields,
                    status="ok",
                    usage=document.get("usage", {}),
                    tool_calls=calls,
                    elapsed_seconds=round(time.monotonic() - started, 3),
                )
                await upstream.aclose()
                return Response(
                    data,
                    media_type="application/json",
                    headers={
                        "X-Gateway-Backend": backend.name,
                        "X-Request-ID": request_id,
                    },
                )
            except httpx.HTTPError, StopAsyncIteration, ValueError, TimeoutError:
                if upstream:
                    await upstream.aclose()
                log.write(**fields, status="connection_or_response_error")
        return error("No configured backend could serve this request; see gateway usage log", 503)

    return app
