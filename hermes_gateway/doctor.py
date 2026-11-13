"""Check installed CLI compatibility and the live free-model catalog without inference."""

import json
import platform
import subprocess

from .opencode_agent import ROOT, child_environment, load_agent_settings
from .opencode_models import discover_models
from .programs import find_program


def inspect(command, *arguments):
    result = subprocess.run(
        [command, *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if result.returncode:
        raise RuntimeError(f"CLI check failed: {arguments}")
    return result.stdout


def main():
    settings = load_agent_settings(ROOT / "opencode-gateway.yaml")
    hermes = find_program("hermes")
    opencode = find_program("opencode")
    help_text = inspect(hermes, "--help")
    if not all(flag in help_text for flag in ("--provider", "--model", "--oneshot")):
        raise RuntimeError("Installed Hermes CLI does not expose the required launch flags")
    native_help = inspect(opencode, "run", "--help")
    if not all(
        flag in native_help for flag in ("--model", "--format", "--standalone", "--session")
    ):
        raise RuntimeError("Installed OpenCode needs native run/JSON/standalone support")
    schema = json.loads(inspect(hermes, "config", "get", "_config_version", "--json"))
    if not isinstance(schema, int) or schema < 12:
        raise RuntimeError("Hermes needs a current configuration schema; update Hermes first")
    gateway_help = inspect(hermes, "gateway", "ensure", "--help")
    if "--timeout" not in gateway_help:
        raise RuntimeError("Hermes needs the gateway ensure/startup-timeout interface")
    models = discover_models(opencode, child_environment(), settings.workdir)
    report = {
        "python": platform.python_version(),
        "hermes": inspect(hermes, "--version").strip(),
        "opencode": inspect(opencode, "--version").strip(),
        "free_models": models,
        "inference_tested": False,
        "config_schema": schema,
    }
    (ROOT / ".runtime/preflight.json").write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Preflight passed: {len(models)} free models advertised.")
    print("No model inference was requested. Use -SmokeTest for a live Hermes turn.")


if __name__ == "__main__":
    main()
