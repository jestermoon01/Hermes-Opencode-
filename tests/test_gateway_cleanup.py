"""Check automatic project cleanup without killing unrelated or busy processes."""

import os
import subprocess
from types import SimpleNamespace

import httpx
import pytest

from hermes_gateway import opencode_launcher as launch
from hermes_gateway import processes
from hermes_gateway.opencode_agent import AgentSettings


def command(root, *arguments):
    return subprocess.list2cmdline(
        [
            str(root / ".venv/Scripts/python.exe"),
            "-m",
            "hermes_gateway.opencode_agent",
            *arguments,
        ]
    )


def config(tmp_path):
    return AgentSettings(workdir=tmp_path, log_file=tmp_path / "usage.jsonl", parallel_workers=True)


def test_ownership_requires_exact_project_interpreter_module_and_config(tmp_path):
    root = tmp_path / "my project"
    assert processes.is_project_gateway(command(root), root)
    assert processes.is_project_gateway(command(root, "--resume-task", "a" * 32), root)
    assert processes.is_project_gateway(
        command(root, "--config", str((root / "opencode-gateway.yaml").resolve())),
        root,
    )
    assert not processes.is_project_gateway(command(tmp_path / "other project"), root)
    assert not processes.is_project_gateway(
        command(root).replace("opencode_agent", "launcher"), root
    )
    assert not processes.is_project_gateway(
        command(root, "--config", str(tmp_path / "other.yaml")), root
    )
    assert not processes.is_project_gateway(None, root)


def test_free_port_needs_no_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(launch, "port_in_use", lambda port: False)
    monkeypatch.setattr(launch, "worker_state", lambda *a, **k: pytest.fail("Unneeded request"))
    assert launch.prepare_gateway_port(config(tmp_path)) is None


def test_legacy_gateway_is_cleaned_and_port_release_is_verified(tmp_path, monkeypatch):
    occupied = iter([True, True, False])
    stopped = []
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    monkeypatch.setattr(launch, "port_in_use", lambda port: next(occupied))
    monkeypatch.setattr(launch, "worker_state", lambda *a, **k: None)
    monkeypatch.setattr(launch, "stop_project_gateway", lambda *args: stopped.append(args))
    assert launch.prepare_gateway_port(config(tmp_path)) is None
    assert stopped == [(8787, tmp_path)]


@pytest.mark.parametrize("busy", ["active", "chat_active"])
def test_busy_gateway_is_reused_with_its_settings(tmp_path, monkeypatch, busy):
    saved = config(tmp_path).model_dump(mode="json")
    saved["max_parallel_workers"] = 1
    monkeypatch.setattr(launch, "port_in_use", lambda port: True)
    monkeypatch.setattr(launch, "worker_state", lambda *a, **k: {busy: 1, "settings": saved})
    monkeypatch.setattr(
        launch, "stop_project_gateway", lambda *a: pytest.fail("Killed active work")
    )
    assert launch.prepare_gateway_port(config(tmp_path)) == saved


@pytest.mark.parametrize("race", [False, True])
def test_authenticated_idle_shutdown_is_graceful_and_preserves_new_work(
    tmp_path, monkeypatch, race
):
    settings = config(tmp_path)
    saved = settings.model_dump(mode="json")
    live = [True]
    requests = []

    def handler(request):
        requests.append(request.url.path)
        assert request.headers["Authorization"].startswith("Bearer ")
        if not race:
            live[0] = False
        return httpx.Response(409 if race else 200, json={"status": "stopping"})

    client_class = httpx.Client
    monkeypatch.setattr(
        launch.httpx,
        "Client",
        lambda **kw: client_class(
            transport=httpx.MockTransport(handler),
            **kw,
        ),
    )
    monkeypatch.setattr(launch, "port_in_use", lambda port: live[0])
    monkeypatch.setattr(launch, "worker_state", lambda *a, **k: {"active": 0, "settings": saved})
    monkeypatch.setattr(
        launch, "stop_project_gateway", lambda *a: pytest.fail("Unneeded forced stop")
    )
    result = launch.prepare_gateway_port(settings)
    assert result == (saved if race else None)
    assert requests == ["/workers/shutdown"]


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree cleanup")
@pytest.mark.parametrize("wrapper", [False, True])
def test_only_the_verified_gateway_tree_is_stopped(tmp_path, monkeypatch, wrapper):
    stopped = []
    monkeypatch.setattr(
        processes,
        "windows_port_processes",
        lambda port: [
            {
                "pid": 22,
                "command_line": command(tmp_path),
                "parent": {
                    "pid": 11,
                    "command_line": command(tmp_path) if wrapper else "unrelated.exe",
                },
            }
        ],
    )
    monkeypatch.setattr(processes, "process_identity", lambda pid: "birth")
    monkeypatch.setattr(
        processes.subprocess,
        "run",
        lambda args, **kw: stopped.append(args) or SimpleNamespace(returncode=0),
    )
    processes.stop_project_gateway(8787, tmp_path)
    assert stopped == [["taskkill.exe", "/PID", "11" if wrapper else "22", "/T", "/F"]]


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree cleanup")
def test_unrelated_listener_is_never_stopped(tmp_path, monkeypatch):
    monkeypatch.setattr(
        processes,
        "windows_port_processes",
        lambda port: [
            {
                "pid": 22,
                "command_line": "unrelated.exe",
                "parent": {},
            }
        ],
    )
    monkeypatch.setattr(
        processes.subprocess, "run", lambda *a, **k: pytest.fail("Killed foreign app")
    )
    with pytest.raises(RuntimeError, match="another application"):
        processes.stop_project_gateway(8787, tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree cleanup")
def test_reused_pid_is_not_killed(tmp_path, monkeypatch):
    identities = iter(["old-birth", "new-birth"])
    monkeypatch.setattr(
        processes,
        "windows_port_processes",
        lambda port: [
            {
                "pid": 22,
                "command_line": command(tmp_path),
                "parent": {},
            }
        ],
    )
    monkeypatch.setattr(processes, "process_identity", lambda pid: next(identities))
    monkeypatch.setattr(processes.subprocess, "run", lambda *a, **k: pytest.fail("PID was reused"))
    processes.stop_project_gateway(8787, tmp_path)


def test_previous_hermes_runtime_is_stopped_using_only_project_home(tmp_path, monkeypatch):
    running = iter([True, False])
    calls = []
    monkeypatch.setattr(launch, "existing_home_gateway", lambda home: next(running))

    def spawn(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(wait=lambda timeout: 0)

    monkeypatch.setattr(launch.subprocess, "Popen", spawn)
    launch.prepare_hermes_runtime("hermes.exe", tmp_path)
    assert calls[0][0] == ["hermes.exe", "gateway", "stop"]
    expected = "\\\\?\\" + str(tmp_path.resolve()) if os.name == "nt" else str(tmp_path)
    assert calls[0][1]["env"]["HERMES_HOME"] == expected


def test_timed_out_hermes_shutdown_cleans_its_owned_helper(tmp_path, monkeypatch):
    stopped = []
    monkeypatch.setattr(launch, "existing_home_gateway", lambda home: True)

    def wait(timeout):
        raise subprocess.TimeoutExpired("hermes.exe", timeout)

    process = SimpleNamespace(wait=wait)
    monkeypatch.setattr(launch.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(launch, "stop_owned_process", stopped.append)
    with pytest.raises(RuntimeError, match="did not stop in time"):
        launch.prepare_hermes_runtime("hermes.exe", tmp_path)
    assert stopped == [process]
