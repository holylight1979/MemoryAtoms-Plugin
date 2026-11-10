"""stop_background.py — 等背景工作：Stop 輸入還有背景任務或排程在跑時，把部分閘延到背景回報那次收尾；同一項連延上限之後照擋，每次都寫帳。
由 handlers/stop.py 的 handle_stop 叫用；本檔不 import 入口檔。
"""

import sys
from typing import Any, Dict, List, Tuple
from wg_core import append_guard_log
import i18n   # lib/：給人看的提示走語言表


BG_DEFER_MAX = 5          # 同一個背景項目連續延後幾次之後照擋（STOP_DEFERS_WHILE_BACKGROUND_RUNNING）


BG_LOG = "stop_background"


def _bg_items(input_data: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Stop 輸入裡還在跑的背景工作與排程喚醒 → [(鍵, 一行說明)]；鍵＝task:<id>／cron:<id>。欄位不在或空＝沒有。"""
    out: List[Tuple[str, str]] = []
    for t in input_data.get("background_tasks") or []:
        if isinstance(t, dict):
            out.append((f"task:{t.get('id')}", f"{t.get('type', '')}:{t.get('status', '')}:{str(t.get('description') or '')[:60]}"))
    for c in input_data.get("session_crons") or []:
        if isinstance(c, dict):
            out.append((f"cron:{c.get('id')}", f"cron:{c.get('schedule', '')}:{'recurring' if c.get('recurring') else 'once'}"))
    return out


def _bg_defer(state: Dict[str, Any], input_data: Dict[str, Any], session_id: str, turn_seq: int) -> bool:
    """規則 STOP_DEFERS_WHILE_BACKGROUND_RUNNING：回合是在等背景工作叫醒（background_tasks／session_crons 有項目），不是做完了 → True＝
    這次收尾到寫碼查核為止，其餘的閘延到背景工作回報那次。每個背景項目記連續延後次數（state bg_defer_counts；這次清單裡沒有的＝結束了，
    歸零）；這次的項目全都已延滿 BG_DEFER_MAX 次 → 不再延、照擋（掛著不會結束的 Monitor、/loop 不能讓收尾閘永遠不跑）。
    延後（event=skip）與到上限（event=cap）都寫 guard log 一筆＋stderr 一行。"""
    items = _bg_items(input_data)
    prev = state.get("bg_defer_counts") if isinstance(state.get("bg_defer_counts"), dict) else {}
    if not items:
        if prev:
            state.pop("bg_defer_counts", None)
        return False
    counts = {k: int(prev.get(k, 0) or 0) for k, _ in items}
    defer = any(n < BG_DEFER_MAX for n in counts.values())
    if defer:
        counts = {k: n + 1 for k, n in counts.items()}
    state["bg_defer_counts"] = counts
    event = "skip" if defer else "cap"
    append_guard_log(BG_LOG, {"event": event, "rule": "STOP_DEFERS_WHILE_BACKGROUND_RUNNING", "session_id": session_id,
                              "turn_seq": turn_seq, "items": [d for _, d in items][:5], "counts": counts, "max": BG_DEFER_MAX})
    print(i18n.t("hook.stop_background.deferred", count=len(items)) if defer
          else i18n.t("hook.stop_background.capped", count=len(items), max=BG_DEFER_MAX), file=sys.stderr)
    return defer
