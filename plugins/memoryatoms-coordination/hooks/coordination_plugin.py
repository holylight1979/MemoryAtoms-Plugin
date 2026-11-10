"""coordination_plugin.py — 跨 session 衝突預警掛到核心的插件點（判法全在 wg_coordination.py）。

設定 coordination.enabled 先判，關著就不載 wg_coordination（零 import 成本）。
"""
from __future__ import annotations

import sys
from typing import Any, Dict, Optional

from i18n import t  # lib/（核心 hook 行程已放進 sys.path；本模組只由插件點載入）

EDIT_TOOLS = ("Write", "Edit", "NotebookEdit")

_warned_file: Optional[str] = None      # 這次發警的檔；警告真的送出才記 warn-cache（deny 蓋掉的回合不記）


def _enabled(config: Dict[str, Any]) -> bool:
    return bool((config.get("coordination") or {}).get("enabled", False))


def before_tool(ctx: Dict[str, Any]) -> Optional[str]:
    """PreToolUse：warn-only、fail-open。"""
    global _warned_file
    config, sid, tool_name = ctx["config"], ctx["session_id"], ctx["tool_name"]
    tool_input = ctx["tool_input"]
    if not _enabled(config):
        return None
    try:
        if tool_name in EDIT_TOOLS:
            path = tool_input.get("file_path", "") or tool_input.get("notebook_path", "")
            if not path:
                return None
            from wg_coordination import check_cross_session_conflict, format_conflict_warning
            hit = check_cross_session_conflict(sid, path, config)
            if not hit:
                return None
            _warned_file = path
            return format_conflict_warning(hit)
        if tool_name == "Bash":
            command = tool_input.get("command", "") or ""
            if command:
                from wg_coordination import check_bash_git_finalize
                return check_bash_git_finalize(sid, command, ctx["input_data"].get("cwd", "") or "", config)
    except Exception as e:  # noqa: BLE001
        print(t("ext.coordination.pre_tool_use_failed", error=e), file=sys.stderr)
    return None


def warned(ctx: Dict[str, Any]) -> None:
    """PreToolUse.warned：提示真的送出了才記 warn-cache。"""
    if not _warned_file:
        return
    try:
        from wg_coordination import record_warn_cache
        record_warn_cache(ctx["session_id"], _warned_file)
    except Exception:  # noqa: BLE001
        pass


def after_saved(ctx: Dict[str, Any]) -> Optional[str]:
    """PostToolUse.saved：寫後補償。兩 session 同時首寫時，寫前掃描雙方都看不見對方，落盤後才可見。"""
    config, sid, file_path = ctx["config"], ctx["session_id"], ctx["file_path"]
    if not (_enabled(config) and ctx["tool_name"] in ("Edit", "Write", "NotebookEdit") and file_path):
        return None
    from handlers._shared import _is_ephemeral_path
    from wg_core import _atom_debug_error
    if _is_ephemeral_path(file_path):
        return None
    try:
        from wg_coordination import (
            check_cross_session_conflict, format_late_collision,
            _warn_cache_suppressed, _norm_path,
        )
        hit = check_cross_session_conflict(
            sid, file_path, config,
            entry_window_s=60, use_cache=False, ev="late_collision",
        )
        if not hit:
            return None
        # log 恆記；advisory 僅在 PreToolUse 近期未警過同檔時附加（防洗版）
        suppress_s = float((config.get("coordination") or {}).get("warn_suppress_min", 10)) * 60
        if not _warn_cache_suppressed(sid, _norm_path(file_path), suppress_s):
            return format_late_collision(hit)
    except Exception as e:  # noqa: BLE001
        _atom_debug_error("post_tool_use:late_collision", e)
    return None
