"""wg_ups_timeout.py — UserPromptSubmit 被 Claude Code 逾時丟掉時，下一次 hook 示警（規則 UPS_CANCEL_WARN_NEXT_HOOK）。

Python 跑完了、Claude Code 還是可能判逾時並丟掉輸出（機器過載時，Python 只跑 1 秒的那則也會被記成等了 40 秒）。
這時哨兵已被正常結尾清掉，Python 這邊看不出輸出沒送到；唯一可靠的紀錄是 transcript 的 hook_cancelled。
所以下一次 hook（PostToolBatch 或下一則 UserPromptSubmit）讀 transcript 尾段，看到新的就示警：
給 AI 一行（這則可能缺卡、要查自己查）、給使用者一行 systemMessage，並記 Logs/guard-hook_timing.jsonl kind=cancelled。
只示警、不補送。每批工具都會跑，所以只讀檔尾、用 bytes 比對過濾，看到才解 JSON。
這是記憶注入本身的訊號，不屬寫碼守門家族：守門家族關（插件版）時照常示警。
"""

import json
import sys
from typing import Any, Dict, List, Tuple

from wg_core import WORKFLOW_DIR, append_guard_log
import i18n  # lib/（wg_core 已放進 sys.path）：給人看的提示走語言表

TAIL_BYTES = 256_000     # 夠涵蓋上一則訊息之後的工具結果；更早的不再示警
GUARDIAN_HOOK = "workflow-guardian"   # 只看負責注入的這支；同事件的其他 hook 被取消不影響記憶注入


def _cancelled_rows(transcript_path: str) -> List[Dict[str, Any]]:
    with open(transcript_path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - TAIL_BYTES))
        data = f.read()
    rows = []
    for raw in data.split(b"\n"):
        if b"hook_cancelled" not in raw or b"UserPromptSubmit" not in raw or GUARDIAN_HOOK.encode() not in raw:
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            continue             # 尾段第一行可能被截半
        att = row.get("attachment") if isinstance(row, dict) else None
        if (isinstance(att, dict) and att.get("type") == "hook_cancelled" and att.get("timedOut")
                and att.get("hookEvent") == "UserPromptSubmit" and GUARDIAN_HOOK in str(att.get("command") or "")):
            rows.append(row)
    return rows


def check_ups_cancelled(state: Dict[str, Any], session_id: str, transcript_path: str, via: str) -> Tuple[str, str]:
    """有沒看過的逾時紀錄 → (給 AI 的一行, 給使用者的一行)；沒有 → ("", "")。
    看過的記在 state["ups_cancel_seen"]（呼叫端寫回；只留還在檔尾的，掉出檔尾的本來就不會再讀到）。同一件事不再由哨兵重報，所以順手清哨兵。"""
    if not transcript_path:
        return "", ""
    try:
        rows = _cancelled_rows(transcript_path)
    except FileNotFoundError:
        return "", ""
    except OSError as e:
        print(i18n.t("hook.wg_ups_timeout.read_transcript_failed", etype=type(e).__name__, error=e), file=sys.stderr)
        return "", ""
    seen = state.get("ups_cancel_seen") or []
    new = [r for r in rows if r.get("uuid") not in seen]
    if not new:
        return "", ""
    state["ups_cancel_seen"] = [r.get("uuid") for r in rows]
    waited = max(int(r["attachment"].get("durationMs") or 0) for r in new)
    limit = max(int(r["attachment"].get("timeoutMs") or 0) for r in new)
    append_guard_log("hook_timing", {"kind": "cancelled", "event": "UserPromptSubmit", "session_id": session_id, "via": via,
                                     "count": len(new), "max_duration_ms": waited, "timeout_ms": limit})
    try:
        (WORKFLOW_DIR / "ups-sentinel" / f"{session_id}.json").unlink(missing_ok=True)
    except OSError as e:
        print(i18n.t("hook.wg_ups_timeout.clear_sentinel_failed", etype=type(e).__name__, error=e), file=sys.stderr)
    values = {"count": len(new), "waited": f"{waited / 1000:.0f}", "limit": f"{limit / 1000:.0f}"}
    return i18n.t("hook.wg_ups_timeout.ai_line", **values), i18n.t("hook.wg_ups_timeout.user_line", **values)
