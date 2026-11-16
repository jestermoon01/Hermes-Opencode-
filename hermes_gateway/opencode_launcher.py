"""Launch Hermes with the real OpenCode agent as its coding backend, without LM Studio."""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import yaml

from .launcher import (
    ROOT,
    existing_home_gateway,
    find_program,
    stop_owned_process,
    wait_gateway,
    write_hermes_home,
)
from .opencode_agent import load_agent_settings
from .opencode_models import model_alias


def wait_hermes_http(origin: str, timeout: int):
    """The control runtime can be ready before its lazily loaded HTTP app is warm."""
    url = httpx.URL(origin)
    if url.scheme != "http" or url.host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("Hermes runtime advertised a non-loopback HTTP endpoint")
    deadline = time.monotonic() + timeout
    with httpx.Client(trust_env=False, timeout=10) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get(origin.rstrip("/") + "/api/health")
                if response.status_code == 200 and response.json().get("ok") is True:
                    return
            except httpx.HTTPError, ValueError:
                pass
            time.sleep(0.5)
    raise RuntimeError("Hermes HTTP frontend did not become healthy before its startup deadline")


def choose_startup_model(model_ids: list[str], default: str) -> str:
    if not model_ids:
        raise RuntimeError("No free OpenCode models are available to select")
    if default not in model_ids:
        default = model_ids[0]
    default_number = model_ids.index(default) + 1
    print("\nHermes - choose your OpenCode model\n", flush=True)
    for number, model in enumerate(model_ids, start=1):
        marker = " (saved choice)" if model == default else ""
        print(f"  {number:2}. {model.removeprefix('oc-')}{marker}", flush=True)
    print("\nEnter a number, press Enter for the saved choice, or type q to quit.", flush=True)
    while True:
        try:
            answer = input(f"Model [{default_number}]: ").strip()
        except EOFError as exc:
            raise RuntimeError("Model selection needs input; run in a terminal") from exc
        if answer.lower() in {"q", "quit", "exit"}:
            raise KeyboardInterrupt
        if not answer:
            return default
        if answer.isdecimal() and 1 <= int(answer) <= len(model_ids):
            return model_ids[int(answer) - 1]
        print(f"Choose a number from 1 to {len(model_ids)}, or q to quit.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt")
    parser.add_argument("--gateway-only", action="store_true")
    parser.add_argument("--resume-task")
    options = parser.parse_args()
    settings = load_agent_settings(ROOT / "opencode-gateway.yaml")
    hermes = find_program("hermes", Path(os.environ["LOCALAPPDATA"]) / "hermes/bin/hermes.exe")
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    gateway = hermes_process = None
    stop_runtime = False
    env = None
    endpoint = f"http://127.0.0.1:{settings.port}"
    try:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", settings.port)) == 0:
                raise RuntimeError(f"Port {settings.port} is occupied; close the other stack first")
        with (runtime / "opencode-gateway.log").open("a", encoding="utf-8") as output:
            gateway_command = [sys.executable, "-m", "hermes_gateway.opencode_agent"]
            if options.resume_task:
                gateway_command += ["--resume-task", options.resume_task]
            gateway = subprocess.Popen(
                gateway_command,
                cwd=ROOT,
                stdout=output,
                stderr=output,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        wait_gateway(endpoint, gateway)
        with httpx.Client(trust_env=False, timeout=10) as client:
            response = client.get(endpoint + "/v1/models")
            response.raise_for_status()
            model_ids = [entry["id"] for entry in response.json()["data"]]
        print(f"Hermes -> {endpoint}/v1 -> OpenCode agent ({settings.model})", flush=True)
        if options.gateway_only:
            print("OpenCode agent gateway ready. Press Ctrl+C to stop.", flush=True)
            gateway.wait()
            return 0
        home = runtime / "hermes-home"
        if existing_home_gateway(home):
            raise RuntimeError("This project's Hermes runtime is already running; close it first")
        config_path = home / "config.yaml"
        chosen_model = model_alias(settings.model)
        if config_path.exists():
            previous = config_path.read_text(encoding="utf-8")
            (home / "config.before-opencode.yaml").write_text(previous, encoding="utf-8")
            previous_model = (yaml.safe_load(previous) or {}).get("model", {})
            if isinstance(previous_model, dict) and previous_model.get("default") in model_ids:
                chosen_model = previous_model["default"]
        if options.prompt is None:
            chosen_model = choose_startup_model(model_ids, chosen_model)
            print(f"\nOpening Hermes with {chosen_model.removeprefix('oc-')}...\n", flush=True)
        write_hermes_home(home, endpoint + "/v1", chosen_model, 65536)
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        # The relay exposes memory/skills only; coding always stays in OpenCode.
        config["toolsets"] = ["memory", "skills"] if settings.hermes_tools else []
        config["fallback_providers"] = []
        config["model"]["provider"] = "custom:opencode-agent"
        config["providers"] = {
            "opencode-agent": {
                "api": endpoint + "/v1",
                "api_key": "local-agent-placeholder",
                "api_mode": "chat_completions",
                "discover_models": False,
                "models": {model: {"context_length": 65536} for model in model_ids},
                "request_timeout_seconds": settings.timeout_seconds + 60,
                "stale_timeout_seconds": settings.timeout_seconds + 60,
            }
        }
        config["model_aliases"] = {
            model: {
                "model": model,
                "provider": "custom:opencode-agent",
                "base_url": endpoint + "/v1",
                "api_key": "local-agent-placeholder",
            }
            for model in model_ids
        }
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "HERMES_HOME": "\\\\?\\" + str(home.resolve()) if os.name == "nt" else str(home),
                "OPENAI_BASE_URL": endpoint + "/v1",
                "OPENAI_API_KEY": "local-agent-placeholder",
                "HERMES_INFERENCE_PROVIDER": "custom:opencode-agent",
                "HERMES_INFERENCE_MODEL": chosen_model,
                "PYTHONUTF8": "1",
            }
        )
        print(
            "In Hermes, type /model to choose an OpenCode model. "
            "Use --global on a typed /model command to save your choice.",
            flush=True,
        )
        command = [hermes, "--provider", "custom:opencode-agent", "--model", chosen_model]
        if settings.hermes_tools:
            command += ["--toolsets", "memory,skills"]
        command += ["-z", options.prompt] if options.prompt else ["--cli"]
        stop_runtime = True
        print("Preparing the project-local Hermes runtime...", flush=True)
        ready = subprocess.run(
            [
                hermes,
                "gateway",
                "ensure",
                "--json",
                "--timeout",
                str(settings.hermes_startup_timeout_seconds),
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=settings.hermes_startup_timeout_seconds + 300,
        )
        readiness = json.loads(ready.stdout)
        if ready.returncode or readiness.get("state") != "ready":
            raise RuntimeError(
                "Hermes background runtime did not become ready; check its local logs"
            )
        wait_hermes_http(
            readiness["endpoint"]["api_origin"], settings.hermes_startup_timeout_seconds
        )
        hermes_process = subprocess.Popen(command, cwd=ROOT, env=env)
        return hermes_process.wait()
    except KeyboardInterrupt:
        return 130
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"OpenCode stack error: {exc}", file=sys.stderr)
        return 1
    finally:
        if hermes_process is not None:
            stop_owned_process(hermes_process)
        if stop_runtime and env:
            try:
                subprocess.run(
                    [hermes, "gateway", "stop"],
                    cwd=ROOT,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=45,
                )
            except subprocess.TimeoutExpired:
                print(
                    "Hermes runtime stop timed out; inspect its project-local logs.",
                    file=sys.stderr,
                )
            except OSError as exc:
                print(f"Hermes runtime stop failed: {exc}; gateway cleanup will continue.",
                      file=sys.stderr)
        if gateway is not None:
            stop_owned_process(gateway)


if __name__ == "__main__":
    raise SystemExit(main())
