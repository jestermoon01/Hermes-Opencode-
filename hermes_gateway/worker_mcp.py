"""Small stdio MCP bridge: Hermes executes tools; the gateway owns worker lifetimes.

Uses the 2025-11-25 MCP lifecycle supported by the installed Hermes client.
No provider credentials are read or forwarded.
"""

import json
import os
import sys

import httpx


def handle(message, client):
    method = message.get("method")
    params = message.get("params") or {}
    if method == "initialize":
        requested = params.get("protocolVersion")
        version = (
            requested
            if requested in {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}
            else "2025-11-25"
        )
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "hermes-opencode-workers", "version": "1.0.0"},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        response = client.get("/workers/tools")
        response.raise_for_status()
        return {"tools": response.json()}
    if method == "tools/call":
        try:
            response = client.post(
                "/workers/call",
                json={
                    "name": params.get("name"),
                    "arguments": params.get("arguments", {}),
                },
            )
            response.raise_for_status()
            result = response.json()
            return {"content": [{"type": "text", "text": json.dumps(result)}], "isError": False}
        except httpx.HTTPError as exc:
            detail = (
                exc.response.text[:1500]
                if isinstance(exc, httpx.HTTPStatusError)
                else "Worker gateway is unavailable"
            )
            return {"content": [{"type": "text", "text": detail}], "isError": True}
    if method in {"resources/list", "prompts/list"}:
        return {"resources" if method == "resources/list" else "prompts": []}
    raise ValueError("Method not found")


def serve(source, output, client):
    for line in source:
        message = None
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("Invalid JSON-RPC request")
            if "id" not in message:
                continue
            response = {"jsonrpc": "2.0", "id": message["id"], "result": handle(message, client)}
        except ValueError, TypeError, httpx.HTTPError:
            response = {
                "jsonrpc": "2.0",
                "id": message.get("id") if isinstance(message, dict) else None,
                "error": {"code": -32600, "message": "Invalid request or gateway unavailable"},
            }
        output.write(json.dumps(response) + "\n")
        output.flush()


def main():
    from pathlib import Path
    from urllib.parse import urlparse

    endpoint = os.environ["HERMES_WORKER_ENDPOINT"]
    url = urlparse(endpoint)
    if url.scheme != "http" or url.hostname != "127.0.0.1":
        raise ValueError("Worker endpoint must use loopback HTTP")
    token = Path(os.environ["HERMES_WORKER_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
    with httpx.Client(
        base_url=endpoint, trust_env=False, timeout=40, headers={"Authorization": "Bearer " + token}
    ) as client:
        serve(sys.stdin, sys.stdout, client)


if __name__ == "__main__":
    main()
