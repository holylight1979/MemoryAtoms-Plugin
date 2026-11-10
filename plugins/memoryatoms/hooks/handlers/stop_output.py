"""stop_output.py — 收尾的出口與共用句：輸出 JSON／放行／擋的三個出口（會把連鎖斷路器要給使用者看的尾巴一併帶上）、連鎖斷路器的接線、提醒只留帳的記法、要問使用者的固定句。
由 handlers/stop.py 以及 stop_code_gate、stop_equivalence 叫用；本檔不 import 入口檔。
"""

import sys
import i18n   # lib/：給人看的提示走語言表
import wg_core as _core
from typing import Any, Dict, Iterable, List, Sequence
from wg_core import append_guard_log, write_state
from wg_lazy_re import lazy_module

_rel = lazy_module("wg_release")
chain_store = lazy_module("chain_store")


# 連鎖斷路器在收尾時不擋、要給使用者看的一段（使用者答了三選一的結果、還在等答的提醒）：這次收尾的每個出口都帶上
_CHAIN_TAIL: List[str] = []


def output_json(data: Dict[str, Any]) -> None:
    if _CHAIN_TAIL:
        data = {**data, "systemMessage": "\n".join(x for x in (data.get("systemMessage"), *_CHAIN_TAIL) if x)}
    _core.output_json(data)


def output_nothing() -> None:
    if _CHAIN_TAIL:
        output_json({})
    _core.output_nothing()


def output_block(reason: str) -> None:
    output_json({"decision": "block", "reason": "\n".join([reason, *_CHAIN_TAIL])})


def _unmark(book: Any, key: str, prev: Any) -> None:
    """連鎖斷路器把這次擋改成只記錄：閘在擋之前自己記下的「本回合擋過」標記放回原值（選繼續之後這道閘照常擋一次）。"""
    if not isinstance(book, dict):
        return
    if prev is None:
        book.pop(key, None)
    else:
        book[key] = prev


def _chain_gate(session_id: str, turn: int, gate: str, what: str, input_data: Dict[str, Any], config: Dict[str, Any],
                state: Dict[str, Any], later: Iterable[str] = ()) -> bool:
    """連鎖斷路器（chain_store.gate_fired）：這道閘要擋或記欠了，先記一筆觸發。回 True＝警報後只記錄（呼叫端跳過這道閘的擋與記欠，
    照往下走）；這一次讓警報響 → 寫回 state、送中止通知取代原擋訊息，結束這次收尾；照擋回 False。"""
    r = chain_store.gate_fired(session_id, turn, gate, what, input_data, config, later=later)
    if r.notice:
        write_state(session_id, state)
        output_block(r.notice)
    if r.record_only:
        print(i18n.t("hook.stop_output.chain_record_only", gate=gate), file=sys.stderr)
    return r.record_only


ASK_HINT = i18n.t("hook.stop_output.ask_hint")     # 寫碼查核每次 Stop 都判，用文字發問也會被擋


def _remind_logged(log_name: str, gate: str, rule: str, text: str, session_id: str, turn_seq: int, **extra: Any) -> None:
    """規則 STOP_REMIND_LEDGER_ONLY：這道閘這次判成提醒（gate_mode 回 remind）→ 只留帳（guard log event=remind）＋stderr 一行；
    不擋、不續跑、不開放行題。提醒本身已掛在改程式檔那則工具結果（handlers/post_tool_batch），這裡不重送。"""
    head = next((ln.strip() for ln in str(text or "").splitlines() if ln.strip()), "")
    append_guard_log(log_name, {"event": "remind", "gate": gate, "rule": rule, "session_id": session_id, "turn_seq": turn_seq,
                                "excerpt": head[:200], **extra})
    print(i18n.t("hook.stop_output.remind_only_logged", gate=gate, rule=rule, head=head[:120]), file=sys.stderr)


# 測試會換掉的名字留在模組上（同 post_tool_use 的惰性入口）：呼叫時才轉給守門模組
def user_released(*a: Any, **k: Any) -> Any:
    return _rel.user_released(*a, **k)


def non_interactive_reason(*a: Any, **k: Any) -> Any:
    return _rel.non_interactive_reason(*a, **k)


def _log_waivers(state: Dict[str, Any], session_id: str, turn_seq: int, uturn: Any, gate: str,
                 waived: Sequence[Dict[str, str]]) -> None:
    """使用者說「先這樣／跳過」放行的留帳（guard-harvest_waived）：同一個使用者回合、同一閘、同一件只記一筆
    （同回合被後面的閘擋回再 Stop、宣告行還在，不重記）。記過的鍵存 state harvest_waived_logged[sid]，換回合清掉。"""
    if not waived:
        return
    book = state.setdefault("harvest_waived_logged", {})
    mine = book.get(session_id) if isinstance(book.get(session_id), dict) else {}
    if mine.get("uturn") != uturn:
        mine = {"uturn": uturn, "keys": []}
    logged = 0
    for w in waived:
        key = f"{gate}|{w['item']}"
        if key in mine["keys"]:          # 跨 Stop 與同一批重複的那件都只記一次
            continue
        mine["keys"].append(key)
        logged += 1
        append_guard_log("harvest_waived", {"session_id": session_id, "turn_seq": turn_seq, "gate": gate,
                                            "item": w["item"], "quote": w["quote"], "line": w["line"]})
    book[session_id] = mine
    if logged:
        write_state(session_id, state)
