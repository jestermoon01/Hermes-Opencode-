"""Read process identity without sending signals on Windows."""

import json
import os
import shlex
import subprocess
from pathlib import Path


def windows_port_processes(port):
    """Inspect only listening processes on the requested port and their parents."""
    script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$records = @()
$listeners = @(Get-NetTCPConnection -State Listen -LocalPort ([int]$env:HERMES_STACK_PORT) `
    -ErrorAction SilentlyContinue)
foreach ($ownerId in @($listeners.OwningProcess | Select-Object -Unique)) {
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $ownerId"
    if ($null -eq $owner) { continue }
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($owner.ParentProcessId)"
    $records += @{
        pid = [int]$owner.ProcessId
        command_line = $owner.CommandLine
        parent = @{ pid = [int]$owner.ParentProcessId; command_line = $parent.CommandLine }
    }
}
ConvertTo-Json -InputObject @($records) -Depth 4 -Compress
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        env={**os.environ, "HERMES_STACK_PORT": str(int(port))},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise RuntimeError("Could not identify the process using the gateway port")
    return json.loads(result.stdout)


def is_project_gateway(command_line, root):
    """The project interpreter and exact module must both match; names alone are insufficient."""
    if not isinstance(command_line, str):
        return False
    arguments = shlex.split(command_line, posix=False)
    if len(arguments) < 3 or arguments[1:3] != ["-m", "hermes_gateway.opencode_agent"]:
        return False
    interpreter = Path(arguments[0].strip('"')).resolve()
    if interpreter != (root / ".venv/Scripts/python.exe").resolve():
        return False
    remaining = arguments[3:]
    while remaining:
        if len(remaining) < 2:
            return False
        flag, value = remaining[:2]
        if flag == "--config":
            config = Path(value.strip('"'))
            if (
                not config.is_absolute()
                or config.resolve() != (root / "opencode-gateway.yaml").resolve()
            ):
                return False
        elif flag == "--resume-task":
            if len(value) != 32 or any(character not in "0123456789abcdef" for character in value):
                return False
        else:
            return False
        remaining = remaining[2:]
    return True


def stop_project_gateway(port, root):
    """Stop verified project gateway trees, including legacy venv launcher wrappers."""
    if os.name != "nt":
        raise RuntimeError("Cannot verify ownership of this legacy gateway on this platform")
    owners = windows_port_processes(port)
    if not owners or any(
        not is_project_gateway(owner.get("command_line"), root) for owner in owners
    ):
        raise RuntimeError(
            f"Port {port} belongs to another application; automatic cleanup left it running"
        )
    targets = {}
    for owner in owners:
        parent = owner.get("parent") or {}
        target = parent if is_project_gateway(parent.get("command_line"), root) else owner
        pid = int(target["pid"])
        identity = process_identity(pid)
        if identity == "unverified":
            raise RuntimeError("Could not verify the stale gateway's process identity")
        if identity is not None:
            targets[pid] = identity
    for pid, identity in targets.items():
        if process_identity(pid) != identity:
            continue
        result = subprocess.run(
            ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode and process_identity(pid) == identity:
            raise RuntimeError(f"Windows could not stop the stale project gateway (PID {pid})")


def process_identity(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [
            ctypes.POINTER(wintypes.FILETIME)
        ] * 4
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None if ctypes.get_last_error() == 87 else "unverified"
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return "unverified"
            if code.value != 259:
                return None
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(item) for item in times)):
                return "unverified"
            return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return "unverified"
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except OSError, IndexError:
        return "unverified"
