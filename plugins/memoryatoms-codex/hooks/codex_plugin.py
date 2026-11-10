"""codex_plugin.py — codex 插件掛到核心的部分：只有開場輪替 log（其他事件靠 settings 片段直接起 codex_companion.py）。"""
from __future__ import annotations

from typing import Any, Dict

from wg_core import CLAUDE_DIR, rotate_log_if_oversized


def on_session_start(ctx: Dict[str, Any]) -> None:
    rotate_log_if_oversized(CLAUDE_DIR / "Logs" / "codex-companion.log", max_mb=10)
