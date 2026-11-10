"""changelog_roll_plugin.py — 改到 _CHANGELOG.md 且列數超過門檻 → 背景起 tools/changelog-roll.py。Fail-open。"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

from wg_core import _atom_debug_error
from wg_platform import background_spawn_kwargs  # lib/（wg_core 已放進 sys.path）：背景起子行程的平台分流唯一一份

_CHANGELOG_TABLE_DATA_RE = re.compile(r"^\|\s*\d{4}-\d{2}-\d{2}\s*\|")
TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "changelog-roll.py"


def on_edited(ctx: Dict[str, Any]) -> None:
    file_path, config = ctx["file_path"], ctx["config"]
    try:
        normalized = file_path.replace("\\", "/")
        if not normalized.endswith("/_CHANGELOG.md") and not normalized.endswith("_CHANGELOG.md"):
            return
        if normalized.endswith("_CHANGELOG_ARCHIVE.md"):
            return
        cfg = (config or {}).get("changelog_auto_roll", {}) or {}
        if not cfg.get("enabled", True):
            return
        threshold = int(cfg.get("threshold", 8))
        cl_path = Path(file_path)
        if not cl_path.exists():
            return
        rows = 0
        for line in cl_path.read_text(encoding="utf-8").splitlines():
            if _CHANGELOG_TABLE_DATA_RE.match(line):
                rows += 1
        if rows <= threshold or not TOOL_PATH.exists():
            return
        bg_kwargs: dict = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            **background_spawn_kwargs(),
        }
        subprocess.Popen(
            [sys.executable, str(TOOL_PATH),
             "--changelog", str(cl_path),
             f"--keep={threshold}", "--quiet"],
            **bg_kwargs,
        )
    except Exception as e:  # noqa: BLE001
        _atom_debug_error("post_tool_use:changelog_auto_roll", e)
