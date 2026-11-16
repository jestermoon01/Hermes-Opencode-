"""Verify publishing boundaries and executable resolution on another machine."""

import json

import pytest

from hermes_gateway import launcher, opencode_launcher, programs


def test_setup_paths_are_local_and_environment_override_wins(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    installed = tmp_path / "installed.exe"
    installed.touch()
    override = tmp_path / "override.exe"
    override.touch()
    (runtime / "tools.json").write_text(json.dumps({"opencode": str(installed)}))
    monkeypatch.setattr(programs, "ROOT", tmp_path)
    monkeypatch.setattr(programs.shutil, "which", lambda name: None)
    assert programs.find_program("opencode") == str(installed.resolve())
    monkeypatch.setenv("HERMES_STACK_OPENCODE", str(override))
    assert programs.find_program("opencode") == str(override.resolve())


def test_missing_executable_reports_setup_command(tmp_path, monkeypatch):
    monkeypatch.setattr(programs, "ROOT", tmp_path)
    monkeypatch.setattr(programs.shutil, "which", lambda name: None)
    monkeypatch.delenv("HERMES_STACK_OPENCODE", raising=False)
    with pytest.raises(RuntimeError, match="setup-hermes-opencode.ps1"):
        programs.find_program("opencode")


def test_publish_manifest_contains_only_source():
    root = programs.ROOT
    manifest = json.loads((root / "publish-manifest.json").read_text())
    forbidden = {".runtime", ".tools", ".venv", ".uv-cache", "Protoboost", "horse-3d"}
    for entry in manifest:
        path = root / entry
        assert path.is_file(), entry
        assert path.resolve().is_relative_to(root.resolve()), entry
        assert not forbidden.intersection(path.relative_to(root).parts), entry
        assert path.name not in {"auth.json", "accounts.json", ".env", "tools.json"}, entry


def test_generated_hermes_config_uses_detected_schema(tmp_path, monkeypatch):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    (runtime / "preflight.json").write_text(json.dumps({"config_schema": 53}))
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    home = runtime / "hermes-home"
    launcher.write_hermes_home(home, "http://127.0.0.1:8787/v1", "oc-test-free", 65536)
    import yaml

    assert yaml.safe_load((home / "config.yaml").read_text())["_config_version"] == 53


def test_frontend_readiness_waits_for_actual_health(monkeypatch):
    import httpx

    attempts = []
    client_class = httpx.Client

    def handler(request):
        attempts.append(request.url.path)
        return httpx.Response(200, json={"ok": len(attempts) > 1})

    monkeypatch.setattr(
        opencode_launcher.httpx,
        "Client",
        lambda **kwargs: client_class(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr(opencode_launcher.time, "sleep", lambda delay: None)
    opencode_launcher.wait_hermes_http("http://127.0.0.1:12345", 5)
    assert attempts == ["/api/health", "/api/health"]


@pytest.mark.parametrize("stop_failure", ["blocked", "timeout"])
def test_opencode_startup_failure_still_stops_owned_gateway(
    tmp_path, monkeypatch, capsys, stop_failure
):
    import socket
    import subprocess
    from types import SimpleNamespace

    import httpx

    settings = SimpleNamespace(
        port=8787, model="opencode/test-free", hermes_tools=False,
        timeout_seconds=60, hermes_startup_timeout_seconds=30,
    )
    gateway = object()
    stopped = []
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[2] == "stop" and stop_failure == "timeout":
            raise subprocess.TimeoutExpired(command, 45)
        raise OSError(4551, "Application control blocked this file")

    client_class = httpx.Client
    monkeypatch.setattr(opencode_launcher, "ROOT", tmp_path)
    monkeypatch.setattr(opencode_launcher, "load_agent_settings", lambda path: settings)
    monkeypatch.setattr(opencode_launcher, "find_program", lambda *args: "hermes.exe")
    monkeypatch.setattr(opencode_launcher, "existing_home_gateway", lambda home: False)
    monkeypatch.setattr(socket.socket, "connect_ex", lambda *args: 1)
    monkeypatch.setattr(opencode_launcher, "wait_gateway", lambda *args: None)
    monkeypatch.setattr(opencode_launcher.subprocess, "Popen", lambda *args, **kw: gateway)
    monkeypatch.setattr(opencode_launcher.subprocess, "run", run)
    monkeypatch.setattr(opencode_launcher, "stop_owned_process", stopped.append)
    monkeypatch.setattr(
        opencode_launcher.httpx, "Client",
        lambda **kwargs: client_class(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"data": [{"id": "oc-test-free"}]})
            ), **kwargs,
        ),
    )
    monkeypatch.setattr(opencode_launcher.sys, "argv", ["launcher", "--prompt", "hello"])
    assert opencode_launcher.main() == 1
    assert [command[2] for command in commands] == ["ensure", "stop"]
    assert stopped == [gateway]
    assert "OpenCode stack error:" in capsys.readouterr().err
