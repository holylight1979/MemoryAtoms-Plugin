"""
handlers/pre_tool_use_atom_format.py — 寫記憶卡的格式檢查

做什麼：Write 直接寫 memory/*.md 時，內容不符原子格式（缺 frontmatter）或新卡不是 [臨] 起跳 → 回 deny 訊息；
Write／Edit 寫 memory/feedback-* → 回路由提醒（不擋）。
誰叫它：handlers.pre_tool_use 的 handle_pre_tool_use。
"""

import re
from pathlib import Path
from typing import Any, Dict, Optional

import wg_core  # noqa: F401  先載它，lib/ 才在 sys.path 上
import i18n   # lib/：給人看的提示走語言表


_CONFIDENCE_FRONTMATTER_RE = re.compile(r"^- Confidence:\s*(\[[臨觀固]\])", re.MULTILINE)
_SOLID_INLINE_RE = re.compile(r"^- \[固\]", re.MULTILINE)
_OBSERVED_INLINE_RE = re.compile(r"^- \[觀\]", re.MULTILINE)


def _scan_atom_confidence(content: str) -> Dict[str, Any]:
    """Parse confidence signals from atom content."""
    fm = _CONFIDENCE_FRONTMATTER_RE.search(content)
    return {
        "frontmatter": fm.group(1) if fm else None,
        "solid_inline": bool(_SOLID_INLINE_RE.search(content)),
        "observed_inline": bool(_OBSERVED_INLINE_RE.search(content)),
    }


def _check_memory_atom_format(
    tool_name: str, tool_input: Dict[str, Any]
) -> Optional[str]:
    """Memory write must follow atom format AND new atoms must start at [臨]."""
    if tool_name != "Write":
        return None
    file_path_raw = tool_input.get("file_path", "")
    file_path = file_path_raw.replace("\\", "/")
    in_memory = "/.claude/memory/" in file_path or wg_core.under_data_memory_dir(file_path_raw)   # 插件版資料根不叫 .claude
    if not in_memory or not file_path.endswith(".md"):
        return None
    if "/_staging/" in file_path:
        return None
    if "/_pending_review/" in file_path:
        return None
    fname = file_path.rsplit("/", 1)[-1]
    if fname.startswith("_") or fname == "MEMORY.md":
        return None
    if fname == "role.md" and ("/personal/" in file_path or "/roles/" in file_path):
        return None
    full_content = tool_input.get("content", "")
    content_head = full_content[:300]
    required = [
        "- Scope:", "- Confidence:", "- Trigger:",
        "- Last-used:", "- Confirmations:", "- ReadHits:",
    ]
    hits = sum(1 for r in required if r in content_head)
    if hits < 3:
        return i18n.t("hook.pre_tool_use_atom_format.not_atom_format", path=file_path, hits=hits)

    try:
        is_new = not Path(file_path_raw).exists()
    except OSError:
        is_new = True
    if is_new:
        sig = _scan_atom_confidence(full_content)
        fm = sig["frontmatter"]
        if fm and fm != "[臨]":
            return i18n.t("hook.pre_tool_use_atom_format.new_atom_confidence", fname=fname, fm=fm,
                          tentative="[臨]", observed="[觀]", solid="[固]")
        if sig["solid_inline"] or sig["observed_inline"]:
            tags = []
            if sig["solid_inline"]:
                tags.append("[固]")
            if sig["observed_inline"]:
                tags.append("[觀]")
            return i18n.t("hook.pre_tool_use_atom_format.new_atom_inline_tag", fname=fname, tags="/".join(tags),
                          tentative="[臨]")
    return None


def _check_feedback_routing_advisory(
    tool_name: str, tool_input: Dict[str, Any]
) -> Optional[str]:
    """V5+: 偵測 memory/feedback-*.md Write/Edit → 回 advisory（不阻擋）。

    feedback-* atoms 物理居 memory/Failures/<主題>/（舊址 _AIDocs/Failures/ 遷移中）；
    走 atom_write MCP 自動路由。
    """
    if tool_name not in ("Write", "Edit"):
        return None
    fp_raw = tool_input.get("file_path", "") or ""
    fp = fp_raw.replace("\\", "/")
    in_memory = "/.claude/memory/" in fp or (bool(fp_raw) and wg_core.under_data_memory_dir(fp_raw))   # 插件版資料根不叫 .claude
    if not in_memory or not fp.endswith(".md"):
        return None
    fname = fp.rsplit("/", 1)[-1]
    if not fname.startswith("feedback-"):
        return None
    return i18n.t("hook.pre_tool_use_atom_format.feedback_routing", path=fp_raw)
