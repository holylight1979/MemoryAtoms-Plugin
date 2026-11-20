"""wg_phase_alerts.py：開場警告的結構化收口。

每條開場警告記成一筆 {source, target, level, scope, text, fix}，同一件事的去重鍵是 source + "\t" + target
（不用整行文字：行內的筆數、日期、檔名每次都會變）。
- level：open＝開場就報（會馬上影響這場工作或可能遺失資料）；done＝完工時（AI 宣告階段完工、知識收割那一刻）
  一條一行附解法提一次；report＝只寫報告與 log，不進對話。
- scope：project＝本 session 的專案；root＝根層系統（資料根本身）；other＝別的專案（不在本 session 提）。
- text：這次的實際文字（含變動字）；fix：一句白話解法（含指令或要對 AI 說的話）。

產生警告的各處維持原本的判斷，只把「吐出什麼」改成呼叫 alert()：open 的照舊回傳字串進開場輸出，其餘回空字串。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

LEVELS = ("open", "done", "report")
SCOPES = ("project", "root", "other")


def _bucket(state: Dict[str, Any]) -> Dict[str, Any]:
    pa = state.setdefault("phase_alerts", {})
    pa.setdefault("pending", {})
    pa.setdefault("shown", {})
    return pa


def alert(state: Dict[str, Any], source: str, target: str, level: str, scope: str, text: str, fix: str = "") -> str:
    """記一條警告到 state["phase_alerts"]["pending"]（鍵 source\\ttarget，同鍵以這次為準）。
    level=open 回 text（呼叫端照舊放進開場輸出），done／report 回空字串。level、scope 不在清單內當 open／project。"""
    level = level if level in LEVELS else "open"
    scope = scope if scope in SCOPES else "project"
    _bucket(state)["pending"][f"{source}\t{target}"] = {
        "source": source, "target": target, "level": level, "scope": scope, "text": text, "fix": fix,
    }
    return text if level == "open" else ""


def _within(p: Path, base: Any) -> bool:
    """p 等於 base 或在它底下（base 解析不了算否）。"""
    try:
        b = Path(str(base)).resolve()
    except (OSError, ValueError):
        return False
    return p == b or b in p.parents


def scope_of_path(state: Dict[str, Any], path: Any, data_root: Any) -> str:
    """路徑屬於誰：是資料根（或在它底下）→ root；在本 session 的專案根底下 → project；其餘 → other。
    專案根取 state["atom_index"]["project_root"]（開場建候選池時寫入），沒有就用 state["session"]["cwd"]。"""
    try:
        p = Path(str(path)).resolve()
    except (OSError, ValueError):
        return "other"
    if data_root and _within(p, data_root):
        return "root"
    proj = (state.get("atom_index") or {}).get("project_root") or (state.get("session") or {}).get("cwd")
    if proj and _within(p, proj):
        return "project"
    return "other"


# ─── 以下是機制（收口 alert_line、跨 session 帳、完工時組句、提醒模式改走 remind_queue）──────────────

LEDGER_NAME = "phase-alerts-ledger.json"   # workflow/ 底下：鍵 source\ttarget → {first_seen, last_seen, shown_count, last_shown_at, scope, proj}
NTH_FROM = 3          # 連續第幾次提起加「第 N 次」
LEDGER_KEEP_DAYS = 30  # 超過這麼久沒再出現的帳丟掉
REMIND_RULE = "PHASE_ALERT"   # 提醒模式走 wg_overview.remind_queue 時的規則名（同鍵本 session 只送一次）


import wg_core  # noqa: F401  先載入：它把 lib/ 放進 sys.path
import i18n   # lib/：給人看的字走語言表（呼叫寫 i18n.t("字面鍵")，語言表對帳工具才掃得到）


def alert_line(state: Any, lines: list, source: str, target: str, level: str, scope: str, text: str,
               fix: str = "") -> None:
    """alert() 的便利包：open 級的字加進 lines。fix 是翻好的解法句（呼叫端寫 i18n.t("hook.phase_alerts.fix.<id>", ...)，
    語言表對帳工具才看得到鍵）；空字串＝無解法。state 傳 None＝只組字串、不留帳（舊呼叫端照舊把 open 的字放進開場輸出）。"""
    s = alert(state if isinstance(state, dict) else {}, source, target, level, scope, text, fix)
    if s:
        lines.append(s)


def _ledger_path(path: Any = None) -> Path:
    if path:
        return Path(path)
    from wg_core import WORKFLOW_DIR   # 取即時值（測試換得掉）
    return Path(WORKFLOW_DIR) / LEDGER_NAME


def _read_ledger(path: Path) -> Dict[str, Any]:
    import json
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        import sys
        print(i18n.t("hook.phase_alerts.ledger_read_failed", error=f"{type(e).__name__}: {e}"), file=sys.stderr)
        return {}


def _write_ledger(path: Path, data: Dict[str, Any]) -> None:
    import json, os, sys
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    except OSError as e:
        print(i18n.t("hook.phase_alerts.ledger_write_failed", path=path, error=f"{type(e).__name__}: {e}"), file=sys.stderr)


def _now() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


def sync_ledger(state: Dict[str, Any], proj: str, path: Any = None) -> None:
    """新開場結束時對帳：本 session 的 pending（scope 非 other）記進帳（first_seen 沒有就補、last_seen 更新）；
    條件已消失的拿掉（根層項目看全部、專案項目只看同一個專案 proj），讓處理掉後再冒出來從頭算；太久沒出現的丟。"""
    from datetime import datetime, timedelta
    p = _ledger_path(path)
    old = _read_ledger(p)
    pend = _bucket(state)["pending"]
    now = _now()
    new: Dict[str, Any] = {}
    for k, rec in old.items():
        if not isinstance(rec, dict) or k in pend:
            continue
        gone = rec.get("scope") == "root" or (rec.get("scope") == "project" and rec.get("proj") == proj)
        try:
            fresh = datetime.now() - datetime.fromisoformat(str(rec.get("last_seen"))) <= timedelta(days=LEDGER_KEEP_DAYS)
        except ValueError:
            fresh = False
        if not gone and fresh:
            new[k] = rec
    for k, a in pend.items():
        if a.get("scope") == "other":
            continue
        rec = old.get(k) if isinstance(old.get(k), dict) else {}
        new[k] = {"first_seen": rec.get("first_seen") or now, "last_seen": now,
                  "shown_count": int(rec.get("shown_count") or 0), "last_shown_at": rec.get("last_shown_at") or "",
                  "scope": a.get("scope"), "proj": proj}
    if new != old:
        _write_ledger(p, new)


def due_entries(state: Dict[str, Any]) -> list:
    """完工時該提的：level=done、scope 是 project 或 root、本 session 還沒提過。scope=other 一律不提。"""
    pa = _bucket(state)
    return [a for k, a in pa["pending"].items()
            if a.get("level") == "done" and a.get("scope") in ("project", "root") and k not in pa["shown"]]


def consume(state: Dict[str, Any], path: Any = None) -> list:
    """取走 due_entries：標進 state 的 shown、帳的 shown_count+1。回 [(條目, 第幾次提)]；沒有回 []。"""
    due = due_entries(state)
    if not due:
        return []
    p = _ledger_path(path)
    led = _read_ledger(p)
    now = _now()
    out = []
    for a in due:
        k = f"{a['source']}\t{a['target']}"
        rec = led.get(k) if isinstance(led.get(k), dict) else {"first_seen": now, "scope": a.get("scope"), "proj": ""}
        n = int(rec.get("shown_count") or 0) + 1
        rec.update({"last_seen": now, "shown_count": n, "last_shown_at": now})
        led[k] = rec
        _bucket(state)["shown"][k] = now
        out.append((a, n))
    _write_ledger(p, led)
    return out


def render_line(a: Dict[str, Any], n: int = 1) -> str:
    """一條一行：「• <text> → <fix>」；連續第 NTH_FROM 次起後面加「第 N 次」與不處理的後果。"""
    line = i18n.t("hook.phase_alerts.item", text=a.get("text", ""), fix=a["fix"]) if a.get("fix") \
        else i18n.t("hook.phase_alerts.item_nofix", text=a.get("text", ""))
    return line + (i18n.t("hook.phase_alerts.nth", n=n) if n >= NTH_FROM else "")


def render_block(items: list) -> str:
    """附在知識收割擋訊息後面的一段（標題一行＋逐條）；沒有條目回空字串。"""
    if not items:
        return ""
    return "\n" + i18n.t("hook.phase_alerts.title", n=len(items)) + "\n" + "\n".join(render_line(a, n) for a, n in items)


def queue_as_reminders(state: Dict[str, Any], items: list) -> None:
    """收割閘在「提醒模式」只留帳不進模型：改走 wg_overview.remind_queue，下一個工具結果送出（同鍵本 session 只送一次）。"""
    from wg_overview import remind_queue, remind_hit
    for a, n in items:
        what = a.get("text", "") + (i18n.t("hook.phase_alerts.nth", n=n) if n >= NTH_FROM else "")
        remind_queue(state, remind_hit(REMIND_RULE, f"{a['source']}\t{a['target']}", what, a.get("fix", "")))
