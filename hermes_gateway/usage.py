"""Record request metadata and provider-reported usage, never prompts or credentials."""

import json
from datetime import UTC, datetime
from pathlib import Path


class UsageLog:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, **fields):
        entry = {"time": datetime.now(UTC).isoformat(), **fields}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=True) + "\n")


class StreamUsage:
    """Inspect a separate copy of SSE bytes without changing the forwarded stream."""

    def __init__(self):
        self.buffer = b""
        self.usage = {}
        self.done = False
        self.tool_calls = 0

    def feed(self, data: bytes):
        self.buffer += data
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                self.done = True
                continue
            try:
                event = json.loads(payload)
            except ValueError, UnicodeDecodeError:
                continue
            if event.get("usage"):
                self.usage = event["usage"]
            for choice in event.get("choices", []):
                for call in choice.get("delta", {}).get("tool_calls", []):
                    if call.get("id"):
                        self.tool_calls += 1
        if len(self.buffer) > 1024 * 1024:
            raise ValueError("Upstream SSE line exceeded 1 MiB")
