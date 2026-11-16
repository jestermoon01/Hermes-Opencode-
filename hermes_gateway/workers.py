"""Persistent, bounded OpenCode workers controlled by Hermes MCP tools."""

import asyncio
import json
import math
import os
import secrets
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from jsonschema import validate

from .model_router import ModelRouter
from .opencode_quota import OpenCodeRateLimit
from .processes import process_identity
from .task_context import TASK_ID, TaskStore

SERVER_NAME = "opencode_workers"
PREFIX = f"mcp__{SERVER_NAME}__"
ID_SCHEMA = {"type": "string", "pattern": "^[a-f0-9]{32}$"}
TEXT_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 16000}


def tool(name, description, properties, required=()):
    return {
        "name": name,
        "description": description,
        "annotations": {
            "readOnlyHint": name in {"list_workers", "get_worker_status", "wait_for_workers"},
            "openWorldHint": True,
        },
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        },
    }


WORKER_TOOLS = [
    tool(
        "spawn_worker",
        "Start an independent OpenCode worker in an isolated workspace. "
        "Delegate independent parts concurrently. Returns immediately; "
        "inspect results before merging.",
        {"task": TEXT_SCHEMA, "model": {"type": "string"}},
        ("task",),
    ),
    tool("list_workers", "List saved workers, including tasks from earlier gateway sessions.", {}),
    tool(
        "get_worker_status",
        "Read worker progress, result, messages, and workspace path.",
        {"worker_id": ID_SCHEMA},
        ("worker_id",),
    ),
    tool(
        "send_worker_message",
        "Queue a follow-up instruction for an OpenCode worker. "
        "Running workers receive it at their next turn boundary, not mid-command.",
        {"worker_id": ID_SCHEMA, "message": TEXT_SCHEMA},
        ("worker_id", "message"),
    ),
    tool(
        "wait_for_workers",
        "Wait up to 30 seconds for all specified workers to stop running. "
        "Returns current status on timeout; never cancels workers.",
        {
            "worker_ids": {"type": "array", "items": ID_SCHEMA, "minItems": 1, "maxItems": 32},
            "timeout_seconds": {"type": "number", "minimum": 0, "maximum": 30},
        },
        ("worker_ids",),
    ),
    tool(
        "cancel_worker",
        "Cancel a queued or running worker and terminate its owned OpenCode process.",
        {"worker_id": ID_SCHEMA},
        ("worker_id",),
    ),
    tool(
        "resume_worker",
        "Explicitly resume an interrupted, failed, cancelled, or limited worker. "
        "Inspect partial edits first. Completed workers take follow-ups "
        "through send_worker_message.",
        {"worker_id": ID_SCHEMA},
        ("worker_id",),
    ),
]
TOOL_SCHEMAS = {item["name"]: item["inputSchema"] for item in WORKER_TOOLS}
ACTIVE = {"queued", "running"}


def worker_token(directory):
    """A per-project capability shared with the private Hermes MCP subprocess."""
    path = directory / "worker-token"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as output:
            output.write(secrets.token_urlsafe(32))
    except FileExistsError:
        pass
    return path.read_text(encoding="utf-8").strip()


class WorkerManager:
    def __init__(self, settings, command, catalog, quota, log, events, turn_class):
        self.settings, self.command, self.catalog = settings, command, catalog
        self.quota, self.log, self.events, self.turn_class = quota, log, events, turn_class
        self.directory = settings.log_file.parent / "workers"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.records = {}
        self.jobs = {}
        self.slots = asyncio.Semaphore(settings.max_parallel_workers)
        self.changed = asyncio.Event()
        self.closing = False
        self.peak_running = 0
        for path in self.directory.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(record, dict) or record.get("id") != path.stem:
                    raise ValueError("Invalid worker record")
                if not TASK_ID.fullmatch(record["id"]):
                    raise ValueError("Invalid worker ID")
                if record.get("source_workdir") != str(settings.workdir.resolve()):
                    continue
                for key in ("status", "task", "messages", "model"):
                    if key not in record:
                        raise ValueError("Incomplete worker record")
            except ValueError, OSError:
                self.log.write(kind="worker_checkpoint_error", status="ignored", file=path.name)
                continue
            if record["status"] in ACTIVE:
                record["status"] = "interrupted"
                record["error"] = "Gateway stopped; inspect partial work before explicit resume."
            self.records[record["id"]] = record
            self.save(record)

    @property
    def active(self):
        return sum(record["status"] in ACTIVE for record in self.records.values())

    def save(self, record):
        record["updated"] = time.time()
        path = self.directory / f"{record['id']}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
        self.changed.set()

    def get(self, worker_id):
        if not isinstance(worker_id, str) or not TASK_ID.fullmatch(worker_id):
            raise ValueError("Invalid worker ID")
        if worker_id not in self.records:
            raise ValueError("Worker not found")
        return self.records[worker_id]

    def view(self, record):
        return {key: value for key, value in record.items() if key != "checkpoint"}

    async def git(self, *arguments, cwd=None):
        process = await asyncio.create_subprocess_exec(
            "git",
            *arguments,
            cwd=cwd or self.settings.workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        if process.returncode:
            raise RuntimeError(stderr.decode("utf-8", "replace")[:1000])
        return stdout.decode("utf-8", "replace").strip()

    async def snapshot(self, destination):
        root = self.settings.workdir.resolve()
        ignored = {
            ".git",
            ".runtime",
            ".tools",
            ".venv",
            ".uv-cache",
            "node_modules",
            "dist",
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
            ".env",
        }
        total = count = 0
        destination.mkdir(parents=True)
        for parent, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [
                name
                for name in dirs
                if name not in ignored
                and not (Path(parent) / name).is_symlink()
                and not (Path(parent) / name).is_junction()
                and not (Path(parent) / name).resolve().is_relative_to(self.directory.resolve())
            ]
            for name in files:
                source = Path(parent) / name
                if name in ignored or name.startswith(".env.") or source.is_symlink():
                    continue
                total += source.stat().st_size
                count += 1
                if total > 128 * 1024 * 1024 or count > 10000:
                    raise RuntimeError(
                        "Project snapshot exceeds 128 MiB/10000 files; use a Git checkout"
                    )
                target = destination / source.relative_to(root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                if count % 100 == 0:
                    await asyncio.sleep(0)

    async def workspace(self, record):
        # A cancelled workspace preparation may have left a partial copy/worktree.
        # Preserve it and prepare a new generation; no native worker ran there yet.
        destination = self.directory / "workspaces" / (record["id"] + "-" + uuid.uuid4().hex[:8])
        try:
            top = await self.git("rev-parse", "--show-toplevel")
        except RuntimeError, OSError:
            top = None
        if top and Path(top).resolve() == self.settings.workdir.resolve():
            if await self.git("status", "--porcelain"):
                raise RuntimeError("Commit or stash project changes before spawning Git workers")
            destination.parent.mkdir(parents=True, exist_ok=True)
            branch = "hermes-worker/" + destination.name
            await self.git("worktree", "add", "-b", branch, str(destination), "HEAD")
            record["workspace_kind"], record["branch"] = "git-worktree", branch
        else:
            await self.snapshot(destination)
            record["workspace_kind"] = "snapshot"
        record["workdir"] = str(destination.resolve())
        self.save(record)

    def schedule(self, record):
        if self.closing:
            raise ValueError("Gateway is shutting down")
        if self.active >= self.settings.max_queued_workers:
            raise ValueError("Worker queue is full; wait or cancel existing workers")
        record["status"] = "queued"
        record["error"] = None
        if record.get("result"):
            record["previous_result"] = record["result"]
        record["result"] = None
        self.save(record)
        self.jobs[record["id"]] = asyncio.create_task(self.run(record))

    async def dispatch(self, name, arguments):
        if name not in TOOL_SCHEMAS:
            raise ValueError("Unknown worker tool")
        validate(arguments, TOOL_SCHEMAS[name])
        if name == "spawn_worker":
            model = arguments.get("model", self.settings.model)
            model = self.catalog.get(model, model)
            if model not in self.catalog.values():
                raise ValueError("Choose a free model from /v1/models")
            self.quota.check(model)
            record = {
                "id": uuid.uuid4().hex,
                "task": arguments["task"],
                "model": model,
                "source_workdir": str(self.settings.workdir.resolve()),
                "status": "new",
                "messages": [],
                "progress": [],
                "result": None,
                "workdir": None,
                "checkpoint": None,
                "created": time.time(),
            }
            self.schedule(record)
            self.records[record["id"]] = record
            return self.view(record)
        if name == "list_workers":
            return [
                {
                    key: record.get(key)
                    for key in ("id", "task", "status", "model", "workdir", "error", "updated")
                }
                for record in self.records.values()
            ]
        if name == "wait_for_workers":
            if not math.isfinite(arguments.get("timeout_seconds", 30)):
                raise ValueError("Worker wait timeout must be finite")
            records = [self.get(worker_id) for worker_id in arguments["worker_ids"]]
            deadline = asyncio.get_running_loop().time() + arguments.get("timeout_seconds", 30)
            while any(record["status"] in ACTIVE for record in records):
                self.changed.clear()
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(self.changed.wait(), remaining)
                except TimeoutError:
                    break
            return [self.view(record) for record in records]
        record = self.get(arguments["worker_id"])
        if name == "send_worker_message":
            if len(record["messages"]) >= 100:
                raise ValueError("Worker mailbox is full")
            if record["status"] not in ACTIVE | {"completed"}:
                raise ValueError("Resume the interrupted worker before sending follow-ups")
            if record["status"] == "completed":
                self.quota.check(record["model"])
                self.schedule(record)
            record["messages"].append({"message": arguments["message"], "delivered": False})
            self.save(record)
        elif name == "resume_worker":
            if record["status"] in ACTIVE | {"completed"}:
                raise ValueError("Worker is active or completed")
            owned = record.get("process")
            if owned:
                current = process_identity(owned["pid"])
                if current is not None and current in {owned["identity"], "unverified"}:
                    raise ValueError(
                        f"Previous native process {owned['pid']} is still running; "
                        "stop it and inspect partial work before resuming"
                    )
            self.quota.check(record["model"])
            self.schedule(record)
        elif name == "cancel_worker" and record["status"] in ACTIVE:
            job = self.jobs.get(record["id"])
            if job:
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
            record["status"] = "cancelled"
            self.save(record)
        return self.view(record)

    async def run(self, record):
        try:
            async with self.slots:
                record["status"] = "running"
                self.peak_running = max(
                    self.peak_running,
                    sum(item["status"] == "running" for item in self.records.values()),
                )
                self.save(record)
                self.quota.check(record["model"])
                if not record["workdir"]:
                    await self.workspace(record)
                while True:
                    pending = [
                        message for message in record["messages"] if not message["delivered"]
                    ]
                    prompt = (
                        "You are an OpenCode worker delegated by Hermes. "
                        "Complete only your assigned task using native OpenCode tools. "
                        "Do not spawn other agents. Your directory is an isolated workspace, "
                        "not a security sandbox. Do not modify the original project "
                        "or publish changes. "
                        "Report actual files, checks and unresolved issues. "
                        "Messages below are instructions from Hermes.\n\nTASK:\n"
                        + record["task"]
                        + "\n\nFOLLOW-UP MESSAGES "
                        "(retain prior instructions, do not replay completed work):\n"
                        + json.dumps(record["messages"])
                    )
                    parent = record.get("checkpoint")
                    store = TaskStore(self.directory / "checkpoints")
                    checkpoint = store.start(
                        {"messages": [{"role": "user", "content": prompt}]},
                        Path(record["workdir"]),
                        parent,
                    )
                    config = self.settings.model_copy(
                        update={
                            "model": record["model"],
                            "workdir": Path(record["workdir"]),
                            "hermes_tools": False,
                            "parallel_workers": False,
                        }
                    )
                    router = ModelRouter(config, self.catalog, self.quota, self.log, self.events)
                    turn = self.turn_class()
                    record["checkpoint"] = checkpoint.data
                    for message in pending:
                        message["delivered"] = True
                    self.save(record)
                    iterator = router.run(prompt, self.command, checkpoint)
                    try:
                        async for event in iterator:
                            if event.get("type") == "bridge_process":
                                record["process"] = {
                                    "pid": event["pid"],
                                    "identity": event["identity"],
                                }
                            turn.accept(event)
                            record["model"] = checkpoint.data.get("model") or record["model"]
                            record["session_id"] = checkpoint.data.get("session_id")
                            record["native_tools"] = checkpoint.data["tools"][-10:]
                            record["progress"] = checkpoint.data["progress"][-5:]
                            self.save(record)
                    finally:
                        await iterator.aclose()
                    if not turn.final_text:
                        raise RuntimeError("OpenCode worker returned no final answer")
                    checkpoint.save("completed")
                    record["result"] = "\n".join(turn.final_text)[-65536:]
                    if not any(not message["delivered"] for message in record["messages"]):
                        break
                record["status"] = "completed"
        except asyncio.CancelledError:
            record["status"] = "interrupted" if self.closing else "cancelled"
            raise
        except OpenCodeRateLimit as exc:
            record["status"] = "rate_limited"
            record["error"] = f"Provider limit; wait at least {exc.retry_after}s before resume."
        except TimeoutError:
            record["status"] = "timed_out"
            record["error"] = (
                f"OpenCode exceeded its {self.settings.timeout_seconds}s turn timeout; "
                "inspect partial work before explicit resume."
            )
        except Exception as exc:
            record["status"] = "failed"
            record["error"] = str(exc)[:1500]
        finally:
            owned = record.get("process")
            if owned:
                current = process_identity(owned["pid"])
                if current is None or current not in {owned["identity"], "unverified"}:
                    record["process"] = None
            self.save(record)

    async def close(self):
        self.closing = True
        for job in self.jobs.values():
            if not job.done():
                job.cancel()
        await asyncio.gather(*self.jobs.values(), return_exceptions=True)
        for record in self.records.values():
            if record["status"] in ACTIVE:
                record["status"] = "interrupted"
                self.save(record)
