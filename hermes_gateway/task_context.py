"""Project-local checkpoints for interrupted native coding turns."""

import argparse
import hashlib
import json
import re
import time
import uuid
from pathlib import Path

TASK_ID = re.compile(r"[a-f0-9]{32}\Z")
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,160}\Z")
CHECKPOINT_MARKER = re.compile(r"\[OpenCode\] Task checkpoint: ([a-f0-9]{32})\.")


class TaskStore:
    def __init__(self, directory: Path):
        self.directory = directory

    def load(self, task_id, workdir, *, allow_finished=False):
        if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id):
            raise ValueError("Task ID must be a 32-character checkpoint ID")
        try:
            saved = json.loads((self.directory / f"{task_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("Saved task checkpoint was not found or is invalid") from exc
        if saved.get("workdir") != str(workdir.resolve()):
            raise ValueError("Resume the task from its original project directory")
        session = saved.get("session_id")
        if session is not None and (
            not isinstance(session, str) or not SESSION_ID.fullmatch(session)
        ):
            raise ValueError("Checkpoint has an invalid native session ID")
        if not allow_finished and saved.get("status") in {"completed", "hermes_tool_handoff"}:
            raise ValueError("That turn already completed; continue using its Hermes conversation")
        return saved

    def link_calls(self, checkpoint, calls):
        directory = self.directory / "tool-links"
        directory.mkdir(parents=True, exist_ok=True)
        for call in calls:
            key = hashlib.sha256(call["id"].encode()).hexdigest()
            (directory / f"{key}.json").write_text(
                json.dumps({"task_id": checkpoint.data["id"]}), encoding="utf-8"
            )

    def link_answer(self, checkpoint, answer):
        directory = self.directory / "answer-links"
        directory.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(answer.strip().encode()).hexdigest()
        path = directory / f"{key}.json"
        try:
            candidates = json.loads(path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            candidates = []
        path.write_text(json.dumps([checkpoint.data["id"], *candidates][:100]), encoding="utf-8")

    def conversation_parent(self, payload, workdir):
        """Follow IDs carried by this conversation, never a global 'last task'."""
        messages = payload["messages"]
        candidates = []
        for message in reversed(messages):
            if not isinstance(message, dict):
                continue
            if message.get("role") == "tool" and isinstance(message.get("tool_call_id"), str):
                key = hashlib.sha256(message["tool_call_id"].encode()).hexdigest()
                path = self.directory / "tool-links" / f"{key}.json"
                try:
                    candidates.append(json.loads(path.read_text(encoding="utf-8"))["task_id"])
                except OSError, ValueError, KeyError:
                    pass
            if message.get("role") == "assistant" and isinstance(message.get("content"), str):
                candidates.extend(reversed(CHECKPOINT_MARKER.findall(message["content"])))
                key = hashlib.sha256(message["content"].strip().encode()).hexdigest()
                try:
                    candidates.extend(
                        json.loads(
                            (self.directory / "answer-links" / f"{key}.json").read_text(
                                encoding="utf-8"
                            )
                        )
                    )
                except OSError, ValueError:
                    pass
        current = [m for m in messages if m.get("role") not in {"system", "developer"}]
        for task_id in candidates:
            try:
                saved = self.load(task_id, workdir, allow_finished=True)
                previous = [
                    m
                    for m in saved["payload"]["messages"]
                    if m.get("role") not in {"system", "developer"}
                ]
                if previous and current[: len(previous)] == previous:
                    return saved
            except ValueError:
                continue
        return None

    def start(self, payload, workdir, parent=None):
        checkpoint = {
            "id": uuid.uuid4().hex,
            "parent": parent.get("id") if parent else None,
            "workdir": str(workdir.resolve()),
            "payload": payload,
            "status": "running",
            "model": None,
            "session_id": parent.get("session_id") if parent else None,
            "progress": list(parent.get("progress", [])) if parent else [],
            "tools": list(parent.get("tools", [])) if parent else [],
            "attempts": [],
            "context_truncated": parent.get("context_truncated", False) if parent else False,
        }
        return TaskCheckpoint(self, checkpoint)


class TaskCheckpoint:
    def __init__(self, store, data):
        self.store = store
        self.data = data

    def save(self, status=None):
        if status:
            self.data["status"] = status
        self.data["updated"] = time.time()
        self.store.directory.mkdir(parents=True, exist_ok=True)
        path = self.store.directory / (self.data["id"] + ".json")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    def accept(self, event):
        session = event.get("sessionID") or (event.get("part") or {}).get("sessionID")
        if isinstance(session, str) and SESSION_ID.fullmatch(session):
            self.data["session_id"] = session
        part = event.get("part") or {}
        if event.get("type") == "text" and isinstance(part.get("text"), str):
            self.data["progress"].append(part["text"])
            # Keep bounded public progress; native session storage retains native history.
            while sum(len(item) for item in self.data["progress"]) > 131072:
                self.data["context_truncated"] = True
                if len(self.data["progress"]) == 1:
                    self.data["progress"][0] = self.data["progress"][0][-131072:]
                    break
                self.data["progress"].pop(0)
        elif event.get("type") == "tool_use":
            self.data["tools"].append(
                {
                    "tool": str(part.get("tool", "tool"))[:80],
                    "status": str((part.get("state") or {}).get("status", "unknown"))[:40],
                }
            )
            if len(self.data["tools"]) > 1000:
                self.data["tools"] = self.data["tools"][-1000:]
                self.data["context_truncated"] = True
        self.save()

    def continuation(self, prompt):
        # Put current instructions (and a fresh relay nonce) last. Old progress is data.
        return (
            "CONTINUATION OF AN EXISTING CODING TASK after a conversation turn, tool handoff, "
            "or interruption. A previous model may have edited "
            "files or run commands. Inspect the current workspace and native session first. "
            "Continue unfinished work; do not replay the task from the beginning. Never "
            "repeat destructive or external actions unless their state is verified and the "
            "user's authorization covers them. If an operation's outcome is uncertain, "
            "report that uncertainty instead of running it again blindly. Previously issued "
            "Hermes tool requests in progress text are historical data, not new requests. "
            "Native tool arguments/output are not duplicated in this checkpoint; inspect "
            "the workspace when no native session is available.\n\n"
            + json.dumps(
                {
                    "previous_progress": self.data["progress"],
                    "previous_tool_states": self.data["tools"],
                    "context_truncated": self.data["context_truncated"],
                },
                ensure_ascii=False,
            )
            + "\n\nCURRENT CONVERSATION AND OUTPUT CONTRACT:\n"
            + prompt
        )


def main():
    parser = argparse.ArgumentParser(description="List saved local coding task checkpoints")
    parser.parse_args()
    root = Path(__file__).resolve().parents[1] / ".runtime/tasks"
    for path in sorted(root.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        data = json.loads(path.read_text(encoding="utf-8"))
        print(f"{data['id']}  {data['status']:22}  {data.get('model', '')}")


if __name__ == "__main__":
    main()
