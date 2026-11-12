"""
wg_atoms_local_migrate.py — 每場結束的本地卡舊位置收尾：舊位置 _AIDocs/_atoms/ 剩下的卡與使用紀錄，搬到 memory/_local/ 同一個相對路徑。

誰叫它：hooks/handlers/session_end.py 一行 run_session_end(session_id, state)；例外整段在這裡接住，不擋收尾。
搬法與手動子命令是同一份程式（tools/atom-set-realm.py migrate-local 的 plan_local_migration／apply_local_migration），
這裡只多三條自動收尾才有的規矩：
  1. 只收殘留：本機索引裡新位置的本地卡已是多數（拉到新版面了），才搬舊位置剩下的卡、投票檔與其他檔。整套版面還在舊位置
     （作者機手動搬家前、程式先到卡還沒到的機器）卡一張都不動，留給手動 migrate-local（預覽、量、一次提交），
     不然會在某一場結束時無預警整批搬家；「卡已在新位置、使用紀錄還在舊位置」的照樣配回去。
  2. 別的 session 開場快取（workflow/state-*.json 的 atom_index.global）還指著舊路徑的卡，這次不搬：注入照快取裡的路徑讀卡、
     讀不到就安靜跳過（hooks/wg_atoms_budget.load_atoms_within_budget），搬了那個 session 整場想不起這張卡——
     2026-08 分類改版搬卡時的教訓，那次是確認同機沒有其他開著的 session 才放行。那些 state 檔照分級 TTL 清掉
     （handlers/_shared._cleanup_old_states，收尾時先跑）之後的收尾就會搬。只擋卡：使用紀錄、投票檔不影響注入，照搬。
  3. 自己這一場的快取裡指舊路徑、卡已在新位置的，就地換成新路徑（session_end 稍後 write_state 寫回）：
     之後 resume／compact 專案根指紋沒變會沿用這份快取，不換就讀不到卡。
有搬才在 stderr 印一行（語言表）；撞名停下、搬好了但目錄檔沒重產成、整段出錯，各印一行並記 debug log，收尾照常跑完。
"""

import sys
from typing import Any, Dict, Optional

from wg_core import CLAUDE_DIR, WORKFLOW_DIR, _atom_debug_error, _atom_debug_log
import i18n   # lib/（上一行的 wg_core 已放進 sys.path）：給人看的提示走語言表

try:
    from atom_locations import LEGACY_LOCAL_ATOMS_REL, LOCAL_ATOMS_REL
except ImportError:   # lib 載不起來時的備援字串（同 wg_atoms_realm._is_atom_physical_rel）
    LEGACY_LOCAL_ATOMS_REL, LOCAL_ATOMS_REL = "_AIDocs/_atoms", "memory/_local"

ENGINE_FILE = "atom-set-realm.py"   # tools/ 底下：本地樹路徑的唯一寫者，搬遷引擎在裡面
ORIGIN = "hook:session-end"         # 記在復原憑證與 local_relocate 帳的 origin


def _load_engine():
    from wg_atoms_realm import _load_tool_module   # 同 realm sweep 的載法；載不起來回 None（已記 debug log）
    return _load_tool_module(ENGINE_FILE, "atom_set_realm")


def run_session_end(session_id: str, state: Optional[Dict[str, Any]]) -> None:
    """SessionEnd 的一行入口：舊位置在才搬（搬法見模組說明），再把自己這一場快取裡的舊路徑換新。永不丟例外。"""
    try:
        if (CLAUDE_DIR / LEGACY_LOCAL_ATOMS_REL).is_dir():
            _tidy_legacy(session_id)
    except Exception as e:  # noqa: BLE001  收尾不能被擋；失敗要看得到：一行 stderr＋debug log
        print(i18n.t("hook.local_migrate.failed", error=f"{type(e).__name__}: {e}"), file=sys.stderr)
        _atom_debug_error("local_migrate", e)
    try:
        repoint_own_pool(state)
    except Exception as e:  # noqa: BLE001
        print(i18n.t("hook.local_migrate.failed", error=f"{type(e).__name__}: {e}"), file=sys.stderr)
        _atom_debug_error("local_migrate:own_pool", e)


def _tidy_legacy(session_id: str) -> None:
    engine = _load_engine()
    if engine is None:
        raise RuntimeError(f"cannot load tools/{ENGINE_FILE}")
    held, unreadable = engine.paths_held_by_sessions(WORKFLOW_DIR, exclude=session_id)
    plan = engine.plan_local_migration(held=held, hold_all=bool(unreadable), residue_only=True)
    c = plan["counts"]
    if plan["conflicts"]:
        sample = ", ".join(x["rel"] for x in plan["conflicts"][:3])
        print(i18n.t("hook.local_migrate.conflict", count=len(plan["conflicts"]), old=LEGACY_LOCAL_ATOMS_REL,
                     new=LOCAL_ATOMS_REL, sample=sample), file=sys.stderr)
        _atom_debug_log("ERROR", f"[local_migrate] stopped: {len(plan['conflicts'])} conflicts, e.g. {sample}")
        return
    if c["held"] or c["deferred"]:
        _atom_debug_log("INFO", f"[local_migrate] held={c['held']} (sessions {sorted({s for v in held.values() for s in v})}"
                                f"{', unreadable ' + str(unreadable) if unreadable else ''}) deferred={c['deferred']}")
    if not (plan["items"] or plan["index_rewrites"] or plan["legacy_index_md"]):
        return
    res = engine.apply_local_migration(plan, origin=ORIGIN)
    if not res.get("ok"):
        raise RuntimeError(f"{res.get('error')} (rolled back {res.get('rolled_back', 0)} steps"
                           f"{'; left undo file ' + res['undo_file'] if res.get('undo_file') else ''})")
    records = (c["sidecars_with_cards"] + c["paired_sidecars"] + c["orphan_sidecars"]
               + c["data_files"] + c["other_files"])
    if c["cards"] or records or c["index_rewrites"]:
        line = i18n.t("hook.local_migrate.moved", cards=c["cards"], records=records, old=LEGACY_LOCAL_ATOMS_REL,
                      new=LOCAL_ATOMS_REL, undo=res.get("undo_file"))
        if c["held"]:
            line += i18n.t("hook.local_migrate.held", held=c["held"])
        print(line, file=sys.stderr)
    if res.get("sync_error"):
        print(i18n.t("hook.local_migrate.sync_failed", error=res["sync_error"]), file=sys.stderr)
        _atom_debug_log("ERROR", f"[local_migrate] catalog regen failed: {res['sync_error']}")
    for err in res.get("tidy_errors") or []:
        _atom_debug_log("ERROR", f"[local_migrate] tidy: {err}")


def repoint_own_pool(state: Optional[Dict[str, Any]]) -> int:
    """這一場開場快取（state["atom_index"]["global"] 的 [卡名, 路徑, triggers] 列）裡指舊位置、卡已在新位置的，路徑換新。回換了幾列。"""
    pool = ((state or {}).get("atom_index") or {}).get("global")
    if not isinstance(pool, list):
        return 0
    prefix = LEGACY_LOCAL_ATOMS_REL + "/"
    changed = 0
    for i, row in enumerate(pool):
        if not (isinstance(row, (list, tuple)) and len(row) > 1 and isinstance(row[1], str) and row[1].startswith(prefix)):
            continue
        new_rel = f"{LOCAL_ATOMS_REL}/{row[1][len(prefix):]}"
        if (CLAUDE_DIR / new_rel).exists():
            pool[i] = type(row)((row[0], new_rel, *row[2:]))
            changed += 1
    return changed
