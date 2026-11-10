"""wisdom_plugin.py — 反思統計掛到核心的插件點（判法全在 wisdom_engine.py）。"""
from __future__ import annotations

from typing import Any, Dict, List

from wisdom_engine import classify_situation, get_reflection_summary, reflect

_APPROACH_RANK = {"direct": 0, "confirm": 1, "plan": 2}


def on_prompt(ctx: Dict[str, Any]) -> None:
    """只記 approach 給反思統計（自知盲點），不注入：分類不看這則訊息、看整個 session 的改檔數與累積關鍵字，
    改檔一多每則訊息都叫人先進 Plan Mode；要不要先規劃由 LLM 依這次的需求判斷。"""
    state = ctx["state"]
    mod_paths = [m["path"] for m in state.get("modified_files", [])]
    tracker = state.get("topic_tracker", {})
    result = classify_situation({
        "intent": tracker.get("intent_distribution", {}).get("top", ""),
        "keywords": tracker.get("keyword_signals", []),
        "estimated_files": max(len(mod_paths), 1),
    })
    cur = result.get("approach", "direct")
    if _APPROACH_RANK.get(cur, 0) > _APPROACH_RANK.get(state.get("wisdom_approach", "direct"), 0):
        state["wisdom_approach"] = cur


def on_session_start(ctx: Dict[str, Any]) -> List[str]:
    return get_reflection_summary()


def on_session_end(ctx: Dict[str, Any]) -> None:
    reflect(ctx["state"])
