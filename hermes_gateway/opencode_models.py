"""Discover explicitly free model IDs through the installed OpenCode application."""

import subprocess


def free_model(model: str) -> bool:
    return model.startswith("opencode/") and (
        model.endswith("-free") or model == "opencode/big-pickle"
    )


def model_alias(model: str) -> str:
    return "oc-" + model.split("/", 1)[1]


def discover_models(command: str, env: dict[str, str], cwd) -> dict[str, str]:
    result = subprocess.run(
        [command, "models", "--standalone"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=cwd,
        timeout=120,
    )
    if result.returncode:
        raise RuntimeError("OpenCode model discovery failed; no paid/default model was selected")
    models = {line.strip() for line in result.stdout.splitlines() if free_model(line.strip())}
    if not models:
        raise RuntimeError("OpenCode advertised no free models")
    return {model_alias(model): model for model in sorted(models)}
