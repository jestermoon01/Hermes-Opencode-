"""Honor native provider rate limits without retrying or switching around them."""

import json
import math
import time
from pathlib import Path


class OpenCodeRateLimit(RuntimeError):
    def __init__(self, retry_after=60, *, free_limit=False, cached=False):
        super().__init__(
            "OpenCode free usage limit reached" if free_limit else "OpenCode rate limit"
        )
        self.retry_after = max(1, math.ceil(retry_after))
        self.free_limit = free_limit
        self.cached = cached


def native_rate_limit(event):
    detail = event.get("error") or {}
    if detail.get("status") != 429 and detail.get("type") != "provider.free-limit":
        return None
    try:
        wait = float(event.get("retryAfterMs", 60000)) / 1000
        if not math.isfinite(wait) or wait <= 0:
            wait = 60
    except ValueError, TypeError:
        wait = 60
    return OpenCodeRateLimit(wait, free_limit=detail.get("type") == "provider.free-limit")


class QuotaGuard:
    def __init__(self, path: Path):
        self.path = path
        try:
            self.resets = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(self.resets, dict):
                self.resets = {}
        except OSError, ValueError:
            self.resets = {}

    def check(self, model):
        now = time.time()
        for scope in ("all-free-models", model):
            until = self.resets.get(scope, 0)
            if isinstance(until, (int, float)) and math.isfinite(until) and until > now:
                raise OpenCodeRateLimit(
                    until - now, free_limit=scope == "all-free-models", cached=True
                )

    def record(self, model, limit):
        if limit.cached:
            return
        scope = "all-free-models" if limit.free_limit else model
        self.resets[scope] = time.time() + limit.retry_after
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.resets) + "\n", encoding="utf-8")
        temporary.replace(self.path)


def quota_message(limit):
    label = "Free usage limit reached" if limit.free_limit else "Provider rate limit reached"
    return (
        f"[OpenCode] {label}. The turn could not finish; partial edits may exist. "
        f"Wait at least {limit.retry_after} seconds for the provider reset. "
        "No automatic retry or model switch was made.\n"
    )
