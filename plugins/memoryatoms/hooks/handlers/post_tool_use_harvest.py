"""
handlers/post_tool_use_harvest.py — 收割回報核對（記憶本體，插件版也出廠）

做什麼：knowledge_harvest_report（以及收尾報告帶的 harvest_items）逐項核對本 session 的 atom 收據並落帳；
核過了背景同步記憶目錄（vcs_sync 關就不起），核不過回告模型要補。
誰叫它：post_tool_use.handle_post_tool_use 的 knowledge_harvest_report 分支、收尾檢核分支（handlers/post_tool_use_aec 同名取用）。
本檔不 import 守門家族的檔：插件版不帶收尾檢核（AEC），這裡照樣能核收割。
"""

import json
import sys
from typing import Any, Dict, List

import i18n   # lib/：給人看的提示走語言表
from wg_core import _atom_debug_error, WORKFLOW_DIR
from wg_evasion import aec_pending_items
from wg_harvest import apply_report, append_ledger
from wg_lazy_re import lazy_module

_rr = lazy_module("wg_release_rules")


def _write_aec_report_file(session_id: str, turn_seq: int, report: Dict[str, Any]) -> None:
    """落 per-turn 報告檔 workflow/aec-report/<sid>-t<turn>.json（atomic tmp→rename）。

    供 HUD 唯讀輪詢最新卡 + 歷史格瀏覽；港口持有者 glob 子夾供頁、與哪個 session 的 MCP
    跑了 tool 無關（Python 寫 disk，跨 instance 安全）。命名比照 codex-companion
    _assessment_turn_path。Fail-open。"""
    try:
        d = WORKFLOW_DIR / "aec-report"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{session_id}-t{turn_seq}.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
        tmp.replace(p)
    except OSError as e:
        _atom_debug_error("post_tool_use:aec_report_write", e)


def spawn_vcs_sync(*a: Any, **k: Any) -> Any:
    """收割 validated 後背景 commit/push 記憶目錄（wg_vcs_sync）；載不到丟 ImportError，呼叫端照 fail-open 浮 stderr。"""
    from wg_vcs_sync import spawn_vcs_sync as _f
    return _f(*a, **k)


def _apply_harvest(state: Dict[str, Any], session_id: str, input_data: Dict[str, Any], items: Any, note: Any,
                   aec_reject_msgs: List[str], via: str) -> None:
    """收割回報核對（one-writer）：knowledge_harvest_report 與 anti_evasion_report 的 harvest_items（收尾一份報告，
    規則 CLOSING_REPORT_ONE_CALL）共用。items ↔ 本 session 自上次 validated 收割以來的 receipts 逐項核對，落分區＋ledger；
    核不過 → pending（Stop 的 Harvest-Pending 閘擋一次要求補）；via＝要 AI 重送的那支工具名。"""
    turn_seq = int(state.get("turn_seq", 0))
    sec = apply_report(state, session_id, turn_seq, items, note)
    append_ledger(session_id, {
        "at": sec["at"], "session_id": session_id, "turn_seq": turn_seq,
        "validated": sec["validated"], "items": sec["items"], "pending": sec["pending"],
        "retired_paths": sec["retired_paths"], "note": sec["note"],
    }, base_dir=WORKFLOW_DIR)
    if sec["validated"]:
        # 本回合（規則 TURN_SINCE_USER_PROMPT，同伴訊息不另開回合）先 emit 的 AEC 報告：(d) 的記憶收錄帳
        # 已由收割核對過 → 用 (h) 重算 d_pending。(d)/(h) 共用 d_pending，整個 pop 會把 (h)「下一動＝寫 atom」一起放掉。
        aec = state.get("anti_evasion_report") or {}
        if (aec.get("session_id") == session_id and _rr.in_user_turn(aec.get("turn_seq"), state, turn_seq)
                and aec.get("d_pending")):
            still = aec_pending_items("", aec.get("h", ""))
            if still:
                aec["d_pending"] = still
            else:
                aec.pop("d_pending", None)
            _write_aec_report_file(session_id, turn_seq, aec)
        _cwd = state.get("session", {}).get("cwd", "") or input_data.get("cwd", "")
        # retired_paths 只帶本次 validated 且有 ok:true retire receipt 的退役檔（apply_report 算好）。
        try:
            spawn_vcs_sync(session_id, _cwd, reason="harvest", retired_paths=sec["retired_paths"])
        except Exception as e:
            print(f"[Guardian:Harvest] spawn_vcs_sync failed (fail-open): {type(e).__name__}: {e}", file=sys.stderr)
    else:
        aec_reject_msgs.append(
            i18n.t("hook.post_tool_use_aec.harvest_pending", count=len(sec['pending']),
                   items="".join("\n  ✗ " + x for x in sec["pending"][:6]), via=via)
        )
