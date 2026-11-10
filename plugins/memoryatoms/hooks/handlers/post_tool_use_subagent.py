"""
handlers/post_tool_use_subagent.py — 子代理注入歸因記錄

從 post_tool_use.py 拆出。做什麼：Agent／Task 工具回來時，從 tool_response.prompt 的注入標頭回推這次 spawn 注入了哪些 atom，
連同輸出摘要記進 state["subagent_injections"]，供「注入 → 使用 → 結果」歸因。
誰叫它：post_tool_use.handle_post_tool_use 的 Agent／Task 分支；上限常數另有 subagent_start 取用。
"""

import re
from typing import Any, Dict

from wg_core import _now_iso


# 從 sub-agent prompt 的注入 header 回推 atom 清單。
#   header 形如：[WG:SubagentMemory] …… atoms=a,b,c
_SUBAGENT_ATOMS_RE = re.compile(r"\[WG:SubagentMemory\][^\n]*?atoms=([^\n]+)")
_SUBAGENT_INJ_CAP = 50          # state 中保留最近 N 筆 spawn 記錄
_SUBAGENT_SUMMARY_CAP = 400     # agent 輸出摘要字元上限


def _extract_agent_output_summary(tool_response: Dict[str, Any], cap: int = _SUBAGENT_SUMMARY_CAP) -> str:
    """從 Agent tool_response.content 擷取文字摘要。content 為 [{type,text}, ...]。"""
    content = tool_response.get("content", "")
    text = ""
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                parts.append(str(blk.get("text", "")))
            elif isinstance(blk, str):
                parts.append(blk)
        text = "\n".join(parts)
    elif isinstance(content, str):
        text = content
    text = text.strip().replace("\r", " ")
    return text[:cap]


def _record_subagent_injection(state: Dict[str, Any], input_data: Dict[str, Any]) -> bool:
    """記錄某次 sub-agent spawn 注入了哪些 atom + 輸出摘要。回 True 表示有寫入。

    無狀態回推：注入清單來自 tool_response.prompt（注入後完整 prompt）的 blob marker；
    無 marker（本次未注入）→ 不記錄、回 False。
    前景子代理的結果在 tool_response.content；背景子代理只回 status=async_launched、沒有 content，摘要留空，
    子代理收尾時由 SubagentStop 依 agent_id 回填（規則 SUBAGENT_RESULT_FROM_SUBAGENT_STOP，handlers/stop._fill_subagent_result）。
    """
    tr = input_data.get("tool_response", {})
    if not isinstance(tr, dict):
        return False
    prompt = tr.get("prompt", "") or input_data.get("tool_input", {}).get("prompt", "") or ""
    m = _SUBAGENT_ATOMS_RE.search(prompt)
    if not m:
        return False
    atoms = [a.strip() for a in m.group(1).split(",") if a.strip()]
    if not atoms:
        return False

    rec = {
        "agent_id": tr.get("agentId", "") or "",
        "agent_type": tr.get("agentType", "") or "",
        "atoms": atoms,
        "status": tr.get("status", "") or "",
        "output_summary": _extract_agent_output_summary(tr),
        "tool_use_id": input_data.get("tool_use_id", "") or "",
        "turn_seq": int(state.get("turn_seq", 0) or 0),  # 來源回合：Stop 只結算本輪的紀錄
        "at": _now_iso(),
    }
    injections = state.setdefault("subagent_injections", [])
    injections.append(rec)
    if len(injections) > _SUBAGENT_INJ_CAP:
        state["subagent_injections"] = injections[-_SUBAGENT_INJ_CAP:]
    return True
