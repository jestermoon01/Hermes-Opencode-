"""Honor shared quotas; distinguish explicitly model-scoped rate limits."""

import json
import math
import re
import time
from pathlib import Path


class OpenCodeRateLimit(RuntimeError):
    def __init__(self, retry_after=60, *, free_limit=False, cached=False, model_specific=False):
        super().__init__(
            "OpenCode free usage limit reached" if free_limit else "OpenCode rate limit"
        )
        self.retry_after = max(1, math.ceil(retry_after))
        self.free_limit = free_limit
        self.cached = cached
        self.model_specific = model_specific and not free_limit


def native_rate_limit(event):
    detail = event.get("error") or {}
    if (
        detail.get("status") != 429
        and detail.get("type") != "provider.free-limit"
        and detail.get("reason") != "rate_limit"
    ):
        return None
    try:
        wait = float(detail.get("retryAfterMs", event.get("retryAfterMs", 60000))) / 1000
        if not math.isfinite(wait) or wait <= 0:
            wait = 60
    except ValueError, TypeError:
        wait = 60
    # The documented error has a message but no reliable quota-scope field. Only
    # explicit provider wording permits a model switch; generic 429s are shared.
    model_specific = bool(
        re.search(
            r"^\s*(?:per[- ]model|model[- ]specific)\s+(?:rate|usage)\s+limit\b",
            str(detail.get("message", "")),
            re.IGNORECASE,
        )
    )
    return OpenCodeRateLimit(
        wait,
        free_limit=detail.get("type") == "provider.free-limit",
        model_specific=model_specific,
    )


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
        for scope in ("all-free-models", "shared-provider", model):
            until = self.resets.get(scope, 0)
            if isinstance(until, (int, float)) and math.isfinite(until) and until > now:
                raise OpenCodeRateLimit(
                    until - now,
                    free_limit=scope == "all-free-models",
                    cached=True,
                    model_specific=scope == model,
                )

    def record(self, model, limit):
        if limit.cached:
            return
        scope = (
            "all-free-models"
            if limit.free_limit
            else model
            if limit.model_specific
            else "shared-provider"
        )
        self.resets[scope] = time.time() + limit.retry_after
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.resets) + "\n", encoding="utf-8")
        temporary.replace(self.path)


def quota_message(limit):
    label = "Free usage limit reached" if limit.free_limit else "Provider rate limit reached"
    return (
        f"[OpenCode] {label}. The turn could not finish; partial edits may exist. "
        f"Wait at least {limit.retry_after} seconds before trying again; "
        "provider access may remain limited. "
        "Automatic continuation has stopped.\n"
    )
