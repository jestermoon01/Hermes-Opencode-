"""Exercise routing boundaries and tool/stream transparency without model dependencies."""

import json

import httpx
from fastapi.testclient import TestClient

from hermes_gateway.app import create_app
from hermes_gateway.config import Settings


def settings(tmp_path):
    return Settings.model_validate(
        {
            "log_file": tmp_path / "usage.jsonl",
            "backends": [
                {
                    "name": name,
                    "priority": priority,
                    "base_url": f"http://127.0.0.1:{port}/v1",
                    "health_url": f"http://127.0.0.1:{port}/v1/models",
                    "context_limit": 65536,
                    "models": {"hermes-local": "actual-model"},
                }
                for name, priority, port in [("primary", 1, 1234), ("fallback", 2, 1235)]
            ],
        }
    )


def completion():
    return {
        "choices": [{"message": {"role": "assistant", "content": "MODEL_OK"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    }


def test_fallback_preserves_tools_and_logs_usage(tmp_path):
    calls = []
    tool = {"type": "function", "function": {"name": "sum", "parameters": {"type": "object"}}}

    def upstream(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        body = json.loads(request.content)
        assert body["tools"] == [tool]
        assert body["tool_choice"] == "auto"
        assert body["model"] == "actual-model"
        calls.append(request.url.port)
        return (
            httpx.Response(503)
            if request.url.port == 1234
            else httpx.Response(200, json=completion())
        )

    config = settings(tmp_path)
    with TestClient(create_app(config, httpx.MockTransport(upstream))) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "hermes-local",
                "messages": [],
                "tools": [tool],
                "tool_choice": "auto",
            },
        )
        assert response.status_code == 200
        assert response.headers["x-gateway-backend"] == "fallback"
    assert calls == [1234, 1235]
    records = [json.loads(line) for line in config.log_file.read_text().splitlines()]
    assert records[-1]["usage"]["completion_tokens"] == 2
    assert "messages" not in records[-1]


def test_stream_preserves_tool_call_deltas_byte_for_byte(tmp_path):
    events = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "sum", "arguments": '{"a":'},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "2}"}}]},
                    "finish_reason": "tool_calls",
                }
            ]
        },
        {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 5}},
    ]
    wire = b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events)
    wire += b"data: [DONE]\n\n"

    def upstream(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        assert json.loads(request.content)["stream_options"]["include_usage"] is True
        return httpx.Response(200, content=wire, headers={"content-type": "text/event-stream"})

    config = settings(tmp_path)
    with TestClient(create_app(config, httpx.MockTransport(upstream))) as client:
        response = client.post("/v1/chat/completions", json={"messages": [], "stream": True})
        assert response.content == wire
    record = json.loads(config.log_file.read_text().splitlines()[-1])
    assert record["status"] == "ok"
    assert record["usage"]["prompt_tokens"] == 20
    assert record["tool_calls"] == 1


def test_timeout_before_response_uses_fallback(tmp_path):
    calls = []

    def upstream(request):
        if request.method == "GET":
            return httpx.Response(200, json={})
        calls.append(request.url.port)
        if request.url.port == 1234:
            raise httpx.ReadTimeout("test timeout", request=request)
        return httpx.Response(200, json=completion())

    with TestClient(create_app(settings(tmp_path), httpx.MockTransport(upstream))) as client:
        result = client.post("/v1/chat/completions", json={"messages": []})
        assert result.status_code == 200
        assert result.headers["x-gateway-backend"] == "fallback"
    assert calls == [1234, 1235]


def test_partial_stream_failure_does_not_restart_on_fallback(tmp_path):
    calls = []

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            raise httpx.ReadError("test broken stream")

    def upstream(request):
        if request.method == "GET":
            return httpx.Response(200, json={})
        calls.append(request.url.port)
        return httpx.Response(
            200,
            stream=BrokenStream(),
            headers={
                "content-type": "text/event-stream",
            },
        )

    config = settings(tmp_path)
    with TestClient(create_app(config, httpx.MockTransport(upstream))) as client:
        result = client.post("/v1/chat/completions", json={"messages": [], "stream": True})
        assert b"partial" in result.content
        assert b"Upstream stream failed" in result.content
    assert calls == [1234]
    assert json.loads(config.log_file.read_text().splitlines()[-1])["status"] == "stream_error"


def test_rate_limit_cools_down_without_retrying_primary(tmp_path):
    calls = []

    def upstream(request):
        if request.method == "GET":
            return httpx.Response(200, json={})
        calls.append(request.url.port)
        if request.url.port == 1234:
            return httpx.Response(429, headers={"retry-after": "600"})
        return httpx.Response(200, json=completion())

    with TestClient(create_app(settings(tmp_path), httpx.MockTransport(upstream))) as client:
        for _ in range(2):
            assert client.post("/v1/chat/completions", json={"messages": []}).status_code == 200
    assert calls == [1234, 1235, 1235]


def test_all_unhealthy_and_unknown_model_are_clear_errors(tmp_path):
    with TestClient(
        create_app(settings(tmp_path), httpx.MockTransport(lambda request: httpx.Response(503)))
    ) as client:
        assert client.get("/health").status_code == 503
        assert client.post("/v1/chat/completions", json={"messages": []}).status_code == 503
        assert (
            client.post(
                "/v1/chat/completions",
                json={
                    "messages": [],
                    "model": "not-configured",
                },
            ).status_code
            == 404
        )
