"""Bounded continuation across independently limited free models."""

import time

from .opencode_quota import OpenCodeRateLimit


class ModelRouter:
    def __init__(self, settings, catalog, quota, log, events):
        self.settings = settings
        self.catalog = catalog
        self.quota = quota
        self.log = log
        self.events = events

    async def run(self, prompt, command, checkpoint):
        selected = self.settings.model
        choices = self.settings.fallback_models or list(self.catalog.values())
        candidates = list(dict.fromkeys([selected, *choices]))
        candidates = [
            model for model in candidates if model in self.catalog.values() or model == selected
        ]
        cap = self.settings.max_model_switches if self.settings.automatic_model_fallback else 0
        candidates = candidates[: cap + 1]
        yield {"type": "bridge_task", "task_id": checkpoint.data["id"]}
        last_limit = None
        for index, model in enumerate(candidates):
            usage = {"prompt_tokens": 0, "completion_tokens": 0}
            usage_known = False
            tools = []
            state = "interrupted"
            started = time.monotonic()
            checkpoint.data["model"] = model
            attempt = {"model": model, "status": "running"}
            checkpoint.data["attempts"].append(attempt)
            checkpoint.save()
            try:
                self.quota.check(model)
                yield {"type": "bridge_model", "model": model, "switched": index > 0}
                continued = index > 0 or checkpoint.data.get("parent") is not None
                attempt_prompt = checkpoint.continuation(prompt) if continued else prompt
                attempt_settings = self.settings.model_copy(
                    update={
                        "model": model,
                        "native_session_id": checkpoint.data.get("session_id"),
                    }
                )
                iterator = self.events(attempt_settings, command, attempt_prompt)
                try:
                    async for event in iterator:
                        checkpoint.accept(event)
                        part = event.get("part") or {}
                        if event.get("type") == "step_finish" and "tokens" in part:
                            tokens = part["tokens"]
                            cache = tokens.get("cache", {})
                            usage_known = True
                            usage["prompt_tokens"] += (
                                tokens.get("input", 0)
                                + cache.get("read", 0)
                                + cache.get("write", 0)
                            )
                            usage["completion_tokens"] += tokens.get("output", 0)
                        if event.get("type") == "tool_use":
                            tools.append(str(part.get("tool", "tool"))[:80])
                        yield event
                finally:
                    await iterator.aclose()
                state = "ok"
                return
            except OpenCodeRateLimit as limit:
                self.quota.record(model, limit)
                state = "cooldown_skipped" if limit.cached else "rate_limited"
                last_limit = limit
                # Neither account-wide free limits nor unknown 429s may be routed around.
                if not limit.model_specific or index == len(candidates) - 1:
                    raise
                yield {"type": "bridge_switch", "model": model, "retry_after": limit.retry_after}
            except Exception:
                state = "agent_error"
                raise
            finally:
                attempt["status"] = state
                checkpoint.save()
                self.log.write(
                    kind="model_attempt",
                    task_id=checkpoint.data["id"],
                    backend="opencode-agent",
                    model=model,
                    status=state,
                    usage=usage if usage_known else {},
                    native_tools=tools,
                    elapsed_seconds=round(time.monotonic() - started, 3),
                )
        if last_limit:
            raise last_limit
