"""Start project-local services and Hermes; clean up only resources started here."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import yaml

from .config import load_settings
from .programs import find_program

ROOT = Path(__file__).resolve().parents[1]


def lms_json(lms: str, *args):
    result = subprocess.run(
        [lms, *args, "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError(f"lms {' '.join(args)} failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def lms_command(lms: str, *args):
    result = subprocess.run(
        [lms, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    if result.returncode:
        raise RuntimeError(f"lms {' '.join(args)} failed: {result.stderr.strip()}")


def wait_gateway(url: str, process: subprocess.Popen, timeout=180):
    deadline = time.monotonic() + timeout
    with httpx.Client(trust_env=False, timeout=2) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Gateway exited; inspect .runtime/gateway-stderr.log")
            try:
                response = client.get(url + "/health")
                if response.status_code == 200:
                    data = response.json()
                    if data.get("service") == "hermes-local-gateway" and data.get("healthy"):
                        return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
    raise RuntimeError(f"Gateway did not become healthy within {timeout} seconds")


def stop_owned_process(process: subprocess.Popen):
    """Windows venv executables may have a child interpreter; stop the owned tree."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            timeout=15,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def existing_home_gateway(home: Path) -> bool:
    """Only inspect the PID record inside this stack's own data folder."""
    try:
        record = json.loads((home / "gateway.pid").read_text(encoding="utf-8"))
        os.kill(int(record["pid"]), 0)
        return True
    except OSError, ValueError, KeyError:
        return False


def write_hermes_home(path: Path, endpoint: str, model: str, context: int):
    path.mkdir(parents=True, exist_ok=True)
    # This belongs only to this project; never copy the user's config or credential files.
    preflight = ROOT / ".runtime/preflight.json"
    schema = (
        json.loads(preflight.read_text(encoding="utf-8")).get("config_schema", 53)
        if preflight.is_file()
        else 53
    )
    config = {
        "_config_version": schema,
        "model": {
            "provider": "custom",
            "base_url": endpoint,
            "default": model,
            "api_mode": "chat_completions",
            "context_length": context,
        },
        "agent": {"max_turns": 12},
        "terminal": {"backend": "local"},
        "auxiliary": {
            "compression": {"provider": "main"},
            "title_generation": {"provider": "main"},
        },
        "display": {"interface": "cli"},
    }
    (path / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    (path / ".env").touch(exist_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", help="Run Hermes once, then clean up")
    parser.add_argument("--gateway-only", action="store_true", help="Serve until Ctrl+C")
    parser.add_argument("--toolsets", help="Optional Hermes toolsets override")
    args = parser.parse_args()
    settings = load_settings(ROOT / "gateway.yaml")
    native_home = Path(os.environ["LOCALAPPDATA"]) / "hermes"
    lms = find_program("lms", Path.home() / ".lmstudio/bin/lms.exe")
    hermes = find_program("hermes", native_home / "bin/hermes.exe")
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    endpoint = f"http://127.0.0.1:{settings.port}"
    server_started = False
    model_loaded = False
    gateway = None
    hermes_process = None
    hermes_env = None
    stop_hermes_gateway = False
    output = None
    errors = None
    try:
        # Refuse occupied gateway ports, including other stack instances, to avoid shared ownership.
        import socket

        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", settings.port)) == 0:
                raise RuntimeError(
                    f"Port {settings.port} is already in use; close that stack first"
                )
        status = lms_json(lms, "server", "status")
        if not status.get("running"):
            print("Starting LM Studio on 127.0.0.1:1234...", flush=True)
            lms_command(lms, "server", "start", "--port", "1234", "--bind", "127.0.0.1")
            server_started = True
        else:
            # A server started by someone else is reused only if it is loopback-only on our port.
            check = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-Command",
                    "Get-NetTCPConnection -State Listen -LocalPort 1234 -ErrorAction Stop | "
                    "Select-Object -ExpandProperty LocalAddress",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            addresses = check.stdout.split()
            if (
                check.returncode
                or not addresses
                or any(a not in {"127.0.0.1", "::1"} for a in addresses)
            ):
                raise RuntimeError(
                    "Existing LM Studio server must listen only on loopback port 1234"
                )
        loaded = lms_json(lms, "ps")
        if not any(m.get("identifier") == settings.launcher.model_identifier for m in loaded):
            print(f"Loading {settings.launcher.model_key}...", flush=True)
            lms_command(
                lms,
                "load",
                settings.launcher.model_key,
                "--context-length",
                str(settings.launcher.context_length),
                "--identifier",
                settings.launcher.model_identifier,
                "--yes",
            )
            model_loaded = True
        output = (runtime / "gateway-stdout.log").open("a", encoding="utf-8")
        errors = (runtime / "gateway-stderr.log").open("a", encoding="utf-8")
        gateway = subprocess.Popen(
            [sys.executable, "-m", "hermes_gateway", "--config", str(ROOT / "gateway.yaml")],
            cwd=ROOT,
            stdout=output,
            stderr=errors,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        wait_gateway(endpoint, gateway)
        print(f"Gateway ready: {endpoint}/v1 -> LM Studio", flush=True)
        if args.gateway_only:
            print("Press Ctrl+C to stop services started by this launcher.", flush=True)
            while gateway.poll() is None:
                time.sleep(0.5)
            raise RuntimeError("Gateway exited unexpectedly")
        home = runtime / "hermes-home"
        write_hermes_home(home, endpoint + "/v1", settings.model, settings.launcher.context_length)
        env = os.environ.copy()
        env.update(
            {
                "HERMES_HOME": ("\\\\?\\" + str(home.resolve())) if os.name == "nt" else str(home),
                "OPENAI_BASE_URL": endpoint + "/v1",
                "OPENAI_API_KEY": "local-gateway-placeholder",
                "HERMES_INFERENCE_PROVIDER": "custom",
                "HERMES_INFERENCE_MODEL": settings.model,
                "PYTHONUTF8": "1",
            }
        )
        command = [hermes, "--provider", "custom", "--model", settings.model]
        if args.toolsets:
            command += ["--toolsets", args.toolsets]
        command += ["-z", args.prompt] if args.prompt else ["--cli"]
        stop_hermes_gateway = not existing_home_gateway(home)
        hermes_env = env
        print("Launching Hermes with project-local settings...", flush=True)
        hermes_process = subprocess.Popen(command, cwd=ROOT, env=env)
        return hermes_process.wait()
    except KeyboardInterrupt:
        return 130
    except (RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
        print(f"Stack error: {exc}", file=sys.stderr)
        return 1
    finally:
        if hermes_process is not None:
            stop_owned_process(hermes_process)
        if stop_hermes_gateway and hermes_env is not None:
            # Hermes auto-starts a separate background agent runtime even for one-shot runs.
            # Ask its own CLI to stop only this project's newly started runtime.
            try:
                stopped = subprocess.run(
                    [hermes, "gateway", "stop"],
                    cwd=ROOT,
                    env=hermes_env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=45,
                )
                if stopped.returncode:
                    print(
                        "Hermes background runtime did not stop cleanly; check its logs.",
                        file=sys.stderr,
                    )
            except subprocess.TimeoutExpired:
                print(
                    "Hermes background runtime stop timed out; other cleanup will continue.",
                    file=sys.stderr,
                )
        if gateway is not None:
            stop_owned_process(gateway)
        if output:
            output.close()
        if errors:
            errors.close()
        if model_loaded:
            try:
                lms_command(lms, "unload", settings.launcher.model_identifier)
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                print(f"Model cleanup: {exc}", file=sys.stderr)
        if server_started:
            try:
                lms_command(lms, "server", "stop")
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                print(f"Server cleanup: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
