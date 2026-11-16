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
from .opencode_agent import AgentSettings, load_agent_settings
from .opencode_models import model_alias
from .processes import stop_project_gateway
from .workers import SERVER_NAME, WORKER_TOOLS, worker_token


def port_in_use(port):
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def prepare_gateway_port(settings):
    """Clear idle/stale project gateways; retain a busy authenticated worker pool."""
    if not port_in_use(settings.port):
        return None
    endpoint = f"http://127.0.0.1:{settings.port}"
    state = worker_state(endpoint, settings, require_settings=False)
    if state and (state.get("active") or state.get("chat_active")):
        print("Reconnecting to this project's active OpenCode workers.", flush=True)
        return state["settings"]
    if state:
        with httpx.Client(trust_env=False, timeout=5) as client:
            try:
                response = client.post(
                    endpoint + "/workers/shutdown",
                    headers={
                        "Authorization": "Bearer " + worker_token(settings.log_file.parent),
                    },
                )
                if response.status_code == 409:
                    # A task may have started between the status check and shutdown request.
                    fresh = worker_state(endpoint, settings, require_settings=False)
                    if fresh:
                        return fresh["settings"]
                    raise RuntimeError("Project gateway became busy; its tasks were left running")
                response.raise_for_status()
            except httpx.HTTPError:
                pass
        deadline = time.monotonic() + 5
        while port_in_use(settings.port) and time.monotonic() < deadline:
            time.sleep(0.1)
    if port_in_use(settings.port):
        print(f"Cleaning up this project's old gateway on port {settings.port}...", flush=True)
        stop_project_gateway(settings.port, ROOT)
    deadline = time.monotonic() + 10
    while port_in_use(settings.port):
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Port {settings.port} did not become available after cleanup")
        time.sleep(0.1)
    print(f"Port {settings.port} is ready for a fresh gateway.", flush=True)
    return None


def worker_state(endpoint, settings, *, require_settings=True):
    """Authenticate a previously started project gateway before reusing it."""
    if not settings.parallel_workers:
        return None
    try:
        with httpx.Client(trust_env=False, timeout=5) as client:
            response = client.get(
                endpoint + "/workers/ping",
                headers={
                    "Authorization": "Bearer " + worker_token(settings.log_file.parent),
                },
            )
            response.raise_for_status()
            state = response.json()
        expected = settings.model_dump(mode="json", exclude={"resume_task_id", "native_session_id"})
        if state.get("workdir") != str(settings.workdir.resolve()):
            return None
        return state if not require_settings or state.get("settings") == expected else None
    except httpx.HTTPError, ValueError, OSError:
        return None


def configure_worker_tools(config, settings, endpoint):
    toolsets = ["memory", "skills"] if settings.hermes_tools else []
    if getattr(settings, "parallel_workers", False):
        worker_token(settings.log_file.parent)
        config["mcp_servers"] = {
            SERVER_NAME: {
                "command": sys.executable,
                "args": [str(ROOT / "hermes_gateway/worker_mcp.py")],
                "cwd": str(ROOT),
                "env": {
                    "HERMES_WORKER_ENDPOINT": endpoint,
                    "HERMES_WORKER_TOKEN_FILE": str(settings.log_file.parent / "worker-token"),
                },
                "tools": {"include": [item["name"] for item in WORKER_TOOLS]},
            }
        }
        toolsets.append("mcp-" + SERVER_NAME)
        config["agent"]["max_turns"] = 100
    config["toolsets"] = toolsets
    return toolsets


def prepare_hermes_runtime(hermes, home):
    """Use Hermes's own scoped shutdown command before starting a fresh frontend."""
    if not existing_home_gateway(home):
        return
    print("Stopping the previous project-local Hermes runtime...", flush=True)
    env = os.environ.copy()
    env["HERMES_HOME"] = "\\\\?\\" + str(home.resolve()) if os.name == "nt" else str(home)
    process = subprocess.Popen(
        [hermes, "gateway", "stop"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        process.wait(timeout=45)
    except subprocess.TimeoutExpired as exc:
        stop_owned_process(process)
        raise RuntimeError(
            "The previous project-local Hermes runtime did not stop in time"
        ) from exc
    deadline = time.monotonic() + 10
    while existing_home_gateway(home):
        if time.monotonic() >= deadline:
            raise RuntimeError("Hermes could not stop its previous project-local runtime")
        time.sleep(0.1)


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
    parser.add_argument("--stop-gateway", action="store_true")
    options = parser.parse_args()
    settings = load_agent_settings(ROOT / "opencode-gateway.yaml")
    endpoint = f"http://127.0.0.1:{settings.port}"
    if options.stop_gateway:
        if not worker_state(endpoint, settings, require_settings=False):
            print("No matching project worker gateway is available.", file=sys.stderr)
            return 1
        with httpx.Client(trust_env=False, timeout=10) as client:
            response = client.post(
                endpoint + "/workers/shutdown",
                headers={
                    "Authorization": "Bearer " + worker_token(settings.log_file.parent),
                },
            )
        if response.status_code != 200:
            print(
                "Finish or cancel active tasks before stopping the worker gateway.", file=sys.stderr
            )
            return 1
        print("Project worker gateway is stopping.", flush=True)
        return 0
    hermes = find_program("hermes", Path(os.environ["LOCALAPPDATA"]) / "hermes/bin/hermes.exe")
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    gateway = hermes_process = None
    stop_runtime = False
    env = None
    try:
        retained_settings = prepare_gateway_port(settings)
        reuse = retained_settings is not None
        if reuse:
            settings = AgentSettings.model_validate(retained_settings)
            print("Existing settings stay in effect until active tasks finish.", flush=True)
        if reuse and options.resume_task:
            raise RuntimeError(
                "ResumeTask requires a fresh gateway; worker resume uses resume_worker"
            )
        if not reuse:
            output = (runtime / "opencode-gateway.log").open("a", encoding="utf-8")
            gateway_command = [sys.executable, "-m", "hermes_gateway.opencode_agent"]
            if options.resume_task:
                gateway_command += ["--resume-task", options.resume_task]
            with output:
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
            if gateway:
                gateway.wait()
            else:
                while worker_state(endpoint, settings):
                    time.sleep(1)
            return 0
        home = runtime / "hermes-home"
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
        prepare_hermes_runtime(hermes, home)
        write_hermes_home(home, endpoint + "/v1", chosen_model, 65536)
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        toolsets = configure_worker_tools(config, settings, endpoint)
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
        if toolsets:
            command += ["--toolsets", ",".join(toolsets)]
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
                print(
                    f"Hermes runtime stop failed: {exc}; gateway cleanup will continue.",
                    file=sys.stderr,
                )
        if gateway is not None:
            state = (
                worker_state(endpoint, settings)
                if getattr(settings, "parallel_workers", False)
                else None
            )
            if state and state["active"]:
                print(
                    "OpenCode workers are still running in the background. "
                    "Reopen hermes-opencode.bat to reconnect.",
                    flush=True,
                )
            else:
                stop_owned_process(gateway)


if __name__ == "__main__":
    raise SystemExit(main())
