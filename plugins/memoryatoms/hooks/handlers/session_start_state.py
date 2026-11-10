"""
handlers/session_start_state.py — SessionStart 的 compact／resume 接續（state 沿用、重建、接續指標）

做什麼：壓縮或恢復舊 session 時：判斷專案根指紋變了沒（決定沿用還是重建 state）、重建時照搬哪些鍵與回合編號墊底、
找最新交接單給接續指標、組壓縮前已注入卡的內文、算重開舊 session 要重寫多少快取；另在新開場記下 git 工作樹快照。
誰叫它：handlers/session_start.py 的 handle_session_start；其他 hook 的測試也讀 _CARRY_ON_REBUILD。
"""

import sys
from typing import Any, Dict, List, Optional, Tuple

from wg_core import _atom_debug_error
import i18n   # lib/：給人看的提示走語言表


def _root_fingerprint_matches(existing: Dict[str, Any], res) -> bool:
    """resume/compact 能沿用舊 state 的前提：專案根指紋沒變（舊 state 沒指紋＝不符 → 重建）。"""
    if res is None:
        return True
    return (existing.get("atom_index") or {}).get("project_root_fingerprint") == res.fingerprint


_CARRY_ON_REBUILD = (
    "modified_files", "accessed_files", "vcs_queries", "knowledge_queue", "sync_pending",
    "stop_blocked_count", "topic_tracker",
    # A2.4：專案根指紋變了重建 atom_index 時，OverviewHub 的讀檔帳／意圖／Depends 已提示的卡照搬，不重送
    "overview_hub", "turn_intent", "depends_hints",
    # 一 session 一次的旗標也要照搬，否則重建後權威地圖會重送
    "authority_map_injected",
    # 收尾同步提醒的額度與底部一般擋擋過的項目（一 session 一次／同一筆不重擋），不搬的話重建後同一個 session 又提醒一次
    "sync_reminder_count", "sync_block_shown",
    # 回合編號與使用者回合鍵（規則 USER_TURN_KEY_KEPT）：只搬鍵的話 turn_seq 歸零、鍵比 turn_seq 還大；不搬的話之後只進同伴訊息時
    # user_turn 退回 turn_seq、每則同伴訊息都當成新使用者回合（各閘每回合的擋次重來）。照搬的 modified_files
    # 也帶著舊的 turn_seq，編號接著走才對得上
    "turn_seq", "turn_prompts_seq", "user_turn_count", "turn_open", "turn_typed", "exec_turn_seq",
    # 事件序號：modified_files 的 last_seq 照搬了，event_seq 不搬就從頭算，壓縮後的收據 seq 全小於舊的 last_seq、等值證法閘整批略過
    "event_seq",
    # 各閘的「問過／放行／擋過」帳本（鍵都是 session id，compact 不換 session）：不搬的話重建後對帳閘、
    # 收據全部重來，使用者剛選的「放行這一回合」當場失效
    "user_released", "pending_release", "last_block", "verify_receipts",
    "reconcile_gate_turn",
    # 知識收割判欠次數（同一場 session 每兩次擋一次，規則 HARVEST_AT_PHASE_DONE）：不搬的話重建後又從第 1 次算
    "harvest_owed_count",
    # 壓縮前已注入的卡名快照：SessionStart(compact) 沒送成（中途降級）時，PostCompact 要靠它暫存內文給 PostToolBatch 補位
    "pre_compact_injected_atoms",
    # 這一輪 PreCompact 的時間：SessionStart(compact) 跑在 PostCompact 之前，不搬的話重建後判不出「這一輪壓縮」
    # （插件自己在 state 裡的鍵由 plugin.json 的 python.state 登記，carry_on_rebuild 一起搬）
    "pre_compact_snapshot",
)


def _mod_turn_of(m: Dict[str, Any]) -> int:
    try:
        return int(m.get("last_turn_seq") or m.get("turn_seq") or 0)
    except (TypeError, ValueError):
        return 0


def floor_turn_seq(state: Dict[str, Any]) -> None:
    """規則 USER_TURN_KEY_KEPT 的底線：回合編號不倒退。更舊版本寫的 state 沒有 turn_seq，照搬的 modified_files 卻帶著 2..11，
    各閘的「問過／放行」會記在第 0 回合，「改檔回合 > 問過的回合」這類判法永遠成立、同一批改動被一直重問。
    以 modified_files 最大的回合墊底；turn_prompts_seq 不動，下一則 UserPromptSubmit 會自己錨定。"""
    floor = max((_mod_turn_of(m) for m in state.get("modified_files") or [] if isinstance(m, dict)), default=0)
    if floor > int(state.get("turn_seq") or 0):
        state["turn_seq"] = floor


def carry_on_rebuild(existing: Dict[str, Any], state: Dict[str, Any]) -> None:
    """專案根指紋變了重建 state：_CARRY_ON_REBUILD 的鍵與開著的插件登記的鍵（wg_plugins.state_keys）照搬、phase 沿用，
    回合編號墊到不倒退。"""
    import wg_plugins   # hooks/：只讀登記表
    for key in (*_CARRY_ON_REBUILD, *wg_plugins.state_keys()):
        if key in existing:
            state[key] = existing[key]
    state["phase"] = existing.get("phase", "working")
    floor_turn_seq(state)


def _next_phase_pointer(cwd: str, source: str) -> list:
    """壓縮／恢復後的接續指標：找最新的交接單（與 /continue 查詢同一組檔：ups_search.continue_handoff_path，專案層與根層
    staging 的 next-phase*.md 取最新），一行叫模型先 Read 它並覆述現狀＋下一步。壓縮會丟掉對話裡的計畫脈絡；這個檔是
    單一權威狀態，不靠模型記得「該去讀」。找不到就不吵。fail-open。"""
    try:
        from handlers.ups_search import continue_handoff_path
        newest = continue_handoff_path("", cwd)
        if not newest:
            return []
        why = i18n.t("hook.session_start_state.why_compact") if source == "compact" else i18n.t("hook.session_start_state.why_resume")
        return [i18n.t("hook.session_start_state.resume_pointer", why=why, path=newest.as_posix())]
    except Exception as e:  # noqa: BLE001
        _atom_debug_error("session_start:next_phase_pointer", e)
        return []


def _take_git_snapshot(state: Dict[str, Any], cwd: str) -> None:
    """開場快照：記下 cwd 所在 repo 的工作樹與本地分支（Stop 拿來比對，補抓 python 腳本裡開的工作樹這類指令解析看不到的
    建立，見 handlers/aec_ledger.snapshot_entries）。compact／resume 沿用開場那份；不是 repo 就不記。"""
    if (state.get("git_snapshot") or {}).get("repo") or not cwd:
        return
    try:
        from handlers import aec_ledger
        snap = aec_ledger.git_snapshot(cwd)
    except Exception as e:  # noqa: BLE001
        _atom_debug_error("session_start:git_snapshot", e)
        return
    if snap:
        state["git_snapshot"] = snap


def _compact_recovery(existing: Optional[Dict[str, Any]], config: Dict[str, Any]) -> Tuple[str, List[str], List[str]]:
    """壓縮前已注入卡的緊湊內文（規則 COMPACT_REINJECT_AT_SESSION_START）。從壓縮前那份 state 組：名單是 PreCompact 快照
    pre_compact_injected_atoms（SessionStart(compact) 跑在 PostCompact 之前），卡路徑照它的 atom_index——壓縮時 cwd 跟開場
    不同而走重建分支也一樣送得出。組法與預算同 post_compact.recovery_blob。回 (內文, 送出的卡名, 索引查不到的卡名)；
    組不出 → 空，內文交給 PostCompact 暫存、PostToolBatch 補位；出錯另寫 stderr 一行。"""
    if not existing:
        return "", [], []
    try:
        from handlers.post_compact import recovery_blob
        return recovery_blob(existing, config)
    except Exception as e:  # noqa: BLE001
        print(i18n.t("hook.session_start_state.recovery_blob_failed", etype=type(e).__name__, err=e), file=sys.stderr)
        return "", [], []


_RESUME_CACHE_NOTE_MIN_TOKENS = 100_000   # 規則 RESUME_CACHE_REWRITE_NOTE：快取多半失效、重送量到這裡才提


def _resume_cache_note(input_data: Dict[str, Any], source: str) -> str:
    """規則 RESUME_CACHE_REWRITE_NOTE：重開舊 session 要重寫多少快取，一行給使用者（systemMessage）與 Claude（additionalContext）。
    資料是官方 SessionStart(resume／fork) 輸入：context_tokens＝第一個請求要重送的 token 數，prompt_cache_likely_expired＝
    快取多半已失效（閒置超過存留期、或之後壓縮過），seconds_since_last_response＝閒置秒數。快取還在、量不到門檻都不提；
    欄位沒給（舊版 Claude Code、逐字稿還沒有回應）＝估不出，不送。"""
    if source not in ("resume", "fork") or input_data.get("prompt_cache_likely_expired") is not True:
        return ""
    try:
        tokens = int(input_data.get("context_tokens") or 0)
        idle_s = int(input_data.get("seconds_since_last_response") or 0)
    except (TypeError, ValueError):
        return ""
    if tokens < _RESUME_CACHE_NOTE_MIN_TOKENS:
        return ""
    if idle_s >= 3600:
        idle = i18n.t("hook.session_start_state.idle_hours", n=f"{idle_s / 3600:.1f}")
    elif idle_s >= 60:
        idle = i18n.t("hook.session_start_state.idle_minutes", n=idle_s // 60)
    else:
        idle = ""
    return i18n.t("hook.session_start_state.resume_cache", idle=idle, amount=i18n.t("hook.session_start_state.cache_amount", n=round(tokens / 10000)))
