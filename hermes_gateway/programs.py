"""Resolve installed programs without storing machine paths in tracked source."""

import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def find_program(name: str, fallback: Path | None = None) -> str:
    config_path = ROOT / ".runtime/tools.json"
    configured = {}
    if config_path.is_file():
        configured = json.loads(config_path.read_text(encoding="utf-8-sig"))
    override = os.environ.get(f"HERMES_STACK_{name.upper()}") or configured.get(name)
    candidates = [override, shutil.which(name), str(fallback) if fallback else None]
    for value in candidates:
        if value and Path(value).is_file():
            return str(Path(value).resolve())
    raise RuntimeError(
        f"Cannot find {name}. Run setup-hermes-opencode.ps1 with -{name.title()}Path"
    )
