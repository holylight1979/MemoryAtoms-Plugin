"""
handlers/stop.py — Stop hook handler

閘序：
1. Test-Fail Gate（測試未綠 + 宣告完成 → 擋；寫碼守門開或關同一套，不看擋次預算）
1b. Reconcile Gate（收尾宣稱 vs 驗證收據：說修好要 PASS、說量了要 DONE、說試過要有執行、搬程式目錄要有掃描與抽樣、AEC 要 j／k）；
    獨立預算 reconcile_gate.max_blocks_per_turn
1e. Equivalence Gate（G7：重構／效能意圖、本回合改過程式檔、宣告完成 → 等值證法三類與熱路徑一問；自己的預算，擋滿問使用者）
2. Evasion 偵測（軟糾正）／Deferral Gate（退縮歸屬）
3. KnowledgeHarvest Gate（宣告完成 → 要求 knowledge_harvest_report；Harvest-Pending 擋核不過的 item）
4. Scan-Report Gate（宣告完成但缺 anti_evasion_report emit → 硬阻）
5. Sync Reminder Gate（modified_files>0 仍未 commit/push → 軟阻；vcs-sync worker 活鎖中的 root 跳過）
+ 一般 sync block 邏輯
寫碼查核（Reconcile）的預算：每個使用者回合 reconcile_gate.max_blocks_per_turn 次，
擋滿後不自行放行：改送擋滿訊息要模型用 AskUserQuestion 問使用者（wg_release）；使用者在最後一次擋之後放行，才放這一回合並記
guard-reconcile.jsonl user_release，放行的那幾件之後的回合原因還在也不再要求放行。擋滿的待放行只在同一個使用者回合有效。
無法互動（headless／SDK、子代理、裁判子 session）不叫它問：擋滿就放行並留帳。查核自己判定出錯：同一個錯誤每個使用者回合
擋一次，之後放行並留帳。其餘共用 stop_gate_max_blocks 的閘每個使用者回合重新計次，同一回合第 3 次放行。
擋或提醒（寫碼守門；規則 GATE_MODE_TABLE／STOP_REMIND_LEDGER_ONLY，表在 wg_release_rules 檔尾）：每道閘拿到擋訊息後先問 gate_mode／hit_mode：
照擋的只有對帳 (a) 說修好要有 PASS（子代理同一套）；紅測閘不問、一律擋；知識收割同 session 第偶數次判欠才擋（wg_harvest.harvest_send_mode）；其餘判成提醒的閘判定照跑、一回合一次的記號照記，但只留帳（_remind_logged：
guard log event=remind＋stderr 一行），不擋、不續跑、不開放行題、不吃擋滿預算；提醒本身掛在改程式檔那則工具結果（handlers/post_tool_batch）。
總開關關時 gate_mode 一律回 block，行為同 9e476fd0。
等背景工作（規則 STOP_DEFERS_WHILE_BACKGROUND_RUNNING，_bg_defer）：Stop 輸入的 background_tasks／session_crons 有項目 → 紅測閘與寫碼查核照跑，
其餘的閘與效用歸因延到背景工作回報那次收尾；同一個背景項目連延 BG_DEFER_MAX 次之後照擋。每次延後與到上限都寫 guard-stop_background.jsonl。
子代理注入的效用帳（規則 SUBAGENT_RESULT_FROM_SUBAGENT_STOP）：SubagentStop 依 agent_id 把最後回覆填回 subagent_injections 那筆，
Stop 結算看收到結果的回合；背景子代理還沒收尾的標 waiting、不結。寫碼守門關也記（dispatcher 照樣分派 SubagentStop）。
連鎖斷路器（寫碼守門；判定 hooks/wg_chain.py、帳與還原 lib/chain_store.py）：各閘之前問 chain_store.on_stop 一次；每道閘要擋或記欠之前
問 _chain_hold——警報後只記錄（跳過那道閘的擋與記欠、往下走），讓警報響的那一次改送中止通知。沒有警報時各閘照原樣擋。
結構：handle_stop 是目錄，一段一個步驟函式，照上面的閘序排在它下面；步驟之間共用的材料與跨步驟的記號放在 _StopRun
（誰填哪幾欄寫在欄位旁），步驟開頭把要用的欄位取成區域變數。
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from dataclasses import dataclass, field

from wg_core import (
    _ensure_state, _now_iso, write_state, read_state,
    append_guard_log, WORKFLOW_DIR, _atom_debug_error, v6_enabled, coding_turn, guard_family_on,
)
from wg_platform import no_window_flags, fold_path_case  # noqa: E402
import wg_core as _core
import i18n   # lib/（wg_core 已放進 sys.path）：給人看的提示走語言表
from wg_release_rules import in_turn, turn_start, user_turn as _user_turn
from wg_evasion import (
    claims_completion, detect_evasion, deferral_gate_reason,
    get_last_assistant_text, detect_missing_aec_emission,
    get_current_turn_text, read_transcript_tail, get_current_turn_visible_text,
    is_context_exempt_phrase, _aec_blank, closing_check_waiver, completion_threshold_met, dismissed_recently, CLOSING_CHECK_ITEM,
)
from wg_lazy_re import lazy_func, lazy_module
# 守門家族檔（插件版不出廠）的函式：名字原樣留在入口（測試與插件從這裡取、測試照換得掉），呼叫時才載那支
receipts_of = lazy_func("wg_verify_ledger", "receipts_of")
reconcile_cfg = lazy_func("wg_verify_ledger", "reconcile_cfg")
reconcile_gate_parts = lazy_func("wg_verify_ledger", "reconcile_gate_parts")
agent_receipt_key = lazy_func("wg_verify_ledger", "agent_receipt_key")
measure_cmds_of = lazy_func("wg_verify_ledger", "measure_cmds_of")
load_self_runs = lazy_func("wg_verify_ledger", "load_self_runs")
measure_parts = lazy_func("wg_verify_ledger", "measure_parts")
self_run_covered = lazy_func("wg_verify_ledger", "self_run_covered")
net_unchanged_paths = lazy_func("wg_verify_ledger", "net_unchanged_paths")
committed_equal_paths = lazy_func("wg_verify_ledger", "committed_equal_paths")
git_deadline = lazy_func("wg_verify_ledger", "git_deadline")
copied_glob_source = lazy_func("wg_verify_ledger", "copied_glob_source")
git_launcher = lazy_func("wg_verify_ledger", "git_launcher")
from wg_release_rules import (
    parse_hits, budget_exhausted, budget_tick, exhausted_summary, is_worktree_checkout, push_required_verdict,
    dpm_instruction_text, retry_rounds_text, RELOAD_ADVICE_RE, reload_advice_verdict,
    evasion_context_exempt_verdict,
    red_item_label, cmd_run_line, parse_equivalence_lines,
    reason_present, red_units, failure_lines, red_count_label, red_rerun_files_label,
)
from wg_core import user_typed_prompts as _user_typed_prompts
from wg_session_checks import _find_session_transcript
from wg_context_usage import estimate_context_usage
from i18n import t
import wg_plugins
from wg_harvest import harvest_gate_reason, harvest_send_mode, note_owed, pending_gate_reason, vcs_sync_lock_active
from wg_release_rules import format_hit
from wg_release_rules import gate_mode as _gate_mode, parse_hit_rules as _parse_hit_rules, REMIND as _REMIND, BLOCK as _BLOCK
# 寫碼守門的模組第一次用到才載（HOOK_IMPORT_ON_USE）：守門關、子代理收尾、或這次收尾走不到那道閘，就一行都不載
chain_store = lazy_module("chain_store")   # 連鎖斷路器的帳、快照、還原（lib/；判定在 hooks/wg_chain.py）
_rel = lazy_module("wg_release")           # 擋滿放行：放行題、待放行紀錄


def token_warn_payload(state: Dict[str, Any], config: Dict[str, Any], transcript: Any, *, transcript_text: Optional[str] = None) -> str:
    """收尾閘要擋時，附在擋訊息尾巴的話（例：context 快滿）：插件給，沒有插件就是空字串。"""
    return "".join(wg_plugins.emit("Stop.block_note", state=state, config=config, transcript=transcript,
                                   transcript_text=transcript_text))


from wg_hook_timing import lap as _hook_lap, quiet as _hook_quiet
from handlers._shared import (
    _maybe_spawn_user_extract_worker,
    DOCDRIFT_AVAILABLE,
)


aec_ledger = lazy_module("handlers.aec_ledger")   # 殘檔帳本（守門家族）：用到才載
from handlers.stop_output import (  # noqa: F401  搬走的名字原樣留在入口（測試與插件從這裡取）
    ASK_HINT, _CHAIN_TAIL, _chain_gate, _log_waivers, _remind_logged, _unmark, non_interactive_reason, output_block, output_json, output_nothing, user_released,
)
# 守門家族（同上：呼叫時才載）
_detect_uncommitted_files = lazy_func("handlers.stop_sync", "_detect_uncommitted_files")
_drop_synced = lazy_func("handlers.stop_sync", "_drop_synced")
_git_dirty_set = lazy_func("handlers.stop_sync", "_git_dirty_set")
_git_unpushed_roots = lazy_func("handlers.stop_sync", "_git_unpushed_roots")
_norm_for_match = lazy_func("handlers.stop_sync", "_norm_for_match")
_svn_dirty_set = lazy_func("handlers.stop_sync", "_svn_dirty_set")
_sync_block_items = lazy_func("handlers.stop_sync", "_sync_block_items")
_sync_block_keys = lazy_func("handlers.stop_sync", "_sync_block_keys")
_synced_paths = lazy_func("handlers.stop_sync", "_synced_paths")
_turn_mark = lazy_func("handlers.stop_sync", "_turn_mark")
from handlers.stop_usefulness import (  # noqa: F401  搬走的名字原樣留在入口（測試與插件從這裡取）
    _ACCESSED_FILES_CAP, _ATOM_AUDIT_LIST_MAX, _attribute_usefulness, _own_failing_tests, _audit_pointer_atom_consumption, _budgeted_embed_fn, _bump_outcome_stats, _detect_turn_outcome, _harvest_accessed_files, _normalize_read_path, _sub_waiting,
)
# 守門家族（同上：呼叫時才載）
_red_item_lines = lazy_func("handlers.stop_testfail", "_red_item_lines")
_rerun_scope = lazy_func("handlers.stop_testfail", "_rerun_scope")
testfail_message = lazy_func("handlers.stop_testfail", "testfail_message")
_acceptance_regression_hint = lazy_func("handlers.stop_postmortem", "_acceptance_regression_hint")
_dpm_instruction = lazy_func("handlers.stop_postmortem", "_dpm_instruction")
_dpm_marker = lazy_func("handlers.stop_postmortem", "_dpm_marker")
_dpm_trigger_text = lazy_func("handlers.stop_postmortem", "_dpm_trigger_text")
_should_deep_postmortem = lazy_func("handlers.stop_postmortem", "_should_deep_postmortem")
_code_check_text = lazy_func("handlers.stop_code_gate", "_code_check_text")
_code_gate_no_ask = lazy_func("handlers.stop_code_gate", "_code_gate_no_ask")
_judge_error = lazy_func("handlers.stop_code_gate", "_judge_error")
_move_inputs = lazy_func("handlers.stop_code_gate", "_move_inputs")
_verify_gates = lazy_func("handlers.stop_code_gate", "_verify_gates")
_equivalence_applies = lazy_func("handlers.stop_equivalence", "_equivalence_applies")
_equivalence_gate = lazy_func("handlers.stop_equivalence", "_equivalence_gate")
_equivalence_on = lazy_func("handlers.stop_equivalence", "_equivalence_on")
_loop_mutation_receipts = lazy_func("handlers.stop_equivalence", "_loop_mutation_receipts")
from handlers.stop_background import (  # noqa: F401  搬走的名字原樣留在入口（測試與插件從這裡取）
    BG_DEFER_MAX, BG_LOG, _bg_defer, _bg_items,
)
from handlers.stop_mcp_stale import (  # noqa: F401  搬走的名字原樣留在入口（測試與插件從這裡取）
    _MCP_CODE_DIR, _MCP_SEGMENT, _mcp_load_facts, _process_rows, _running_mcp_pids, _session_mcp_pids, _session_touched_mcp,
)
# 守門家族（同上：呼叫時才載）
_record_prompt_judgments = lazy_func("handlers.stop_declarations", "_record_prompt_judgments")
_record_tracking_lines = lazy_func("handlers.stop_declarations", "_record_tracking_lines")
_deferral_covers = lazy_func("handlers.stop_evasion", "_deferral_covers")
_evasion_exempt_verdict = lazy_func("handlers.stop_evasion", "_evasion_exempt_verdict")


# 守門家族檔的常數（插件版不出廠）：入口外的人照舊從這裡取（`stop.EQ_GATE`），第一次取才載那支
_GUARD_CONSTS = {
    "EQ_GATE": ("handlers.stop_equivalence", "EQ_GATE"),
    "RERUN_SCOPE_SHOWN_MAX": ("handlers.stop_testfail", "RERUN_SCOPE_SHOWN_MAX"),
    "_DEFERRAL_SENTENCE_RE": ("handlers.stop_evasion", "_DEFERRAL_SENTENCE_RE"),
    "_NO_WINDOW": ("handlers.stop_sync", "_NO_WINDOW"),
}


def __getattr__(name: str) -> Any:
    if name in _GUARD_CONSTS:
        import importlib
        module, attr = _GUARD_CONSTS[name]
        return getattr(importlib.import_module(module), attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# walk-up 定 VCS 根（零 subprocess）；與 pre_tool_use／session_start／tools 共用同一支
from wg_core import find_vcs_root as _find_vcs_root, find_vcs_root_ex as _find_vcs_root_ex  # noqa: E402


CLOSING_ONE_CALL = i18n.t("hook.stop.closing_one_call")
# 寫碼守門開時「後面還會擋」預覽與改檔那則提醒用的做法
CLOSING_CHECK_FIX = i18n.t("hook.stop.closing_check_fix")


def _scan_report_gate_on(config: Dict[str, Any]) -> bool:
    """規則 CLOSING_RITUAL_SWITCHES：aec.scan_report_gate=false → 沒交收尾檢核不擋收尾。沒設＝照擋。"""
    return (config.get("aec") or {}).get("scan_report_gate", True) is not False


LEGACY_PENDING = i18n.t("hook.stop.legacy_pending")


SUBAGENT_RESULT_CAP = 400   # 子代理結果摘要字元上限（與 post_tool_use._SUBAGENT_SUMMARY_CAP 同長）


def _fill_subagent_result(state: Dict[str, Any], agent_id: str, text: Any) -> bool:
    """規則 SUBAGENT_RESULT_FROM_SUBAGENT_STOP：子代理收尾，依 agent_id 找 subagent_injections 最後一筆還沒結算的紀錄，
    output_summary 填最後回覆（截 SUBAGENT_RESULT_CAP 字）、status 改 completed、result_turn_seq 記收到結果的回合、拿掉 waiting。
    對不到（內部子代理、這個子代理沒注入卡、早就結算過）回 False。"""
    for rec in reversed(state.get("subagent_injections") or []):
        if isinstance(rec, dict) and rec.get("agent_id") == agent_id and not rec.get("attributed"):
            rec["output_summary"] = str(text or "").strip().replace("\r", " ")[:SUBAGENT_RESULT_CAP]
            rec["status"] = "completed"
            rec["result_turn_seq"] = int(state.get("turn_seq", 0) or 0)
            rec.pop("waiting", None)
            return True
    return False


def handle_subagent_stop(input_data: Dict[str, Any], config: Dict[str, Any]) -> None:
    """SubagentStop：先把子代理的最後回覆填回它的注入紀錄（效用帳，寫碼守門關也記）；寫碼守門開才往下看子代理自己改的檔
    （agent_modified_files[agent_id]）與自己的收據（agent_receipt_key），寫碼查核照預算擋。"""
    session_id = input_data.get("session_id", "") or ""
    agent_id = str(input_data.get("agent_id") or "")
    state = read_state(session_id) if session_id and agent_id else None
    if state and _fill_subagent_result(state, agent_id, input_data.get("last_assistant_message")):
        write_state(session_id, state)
    if not (v6_enabled(config) and guard_family_on(config)):   # 子代理寫碼查核屬守門家族
        output_nothing()
        return
    own = list(((state or {}).get("agent_modified_files") or {}).get(agent_id) or [])
    unknown = list(((state or {}).get("agent_unknown_writes") or {}).get(agent_id) or [])
    _code_gate_no_ask(input_data, config, state, agent_receipt_key(session_id, agent_id), own,
                      input_data.get("agent_transcript_path") or "", i18n.t("hook.stop.who_subagent"),
                      {"event": "subagent_block", "agent_id": agent_id}, unknown)


def handle_judge_stop(input_data: Dict[str, Any], config: Dict[str, Any]) -> None:
    """裁判子 session（CLAUDE_COMPANION_JUDGE=1）的 Stop：dispatcher 其他事件照舊早退，Stop 只跑寫碼查核閘，照預算擋。
    改檔、未知寫入與收據來自 post_tool_use.handle_judge_post_tool_use 的最小記帳。"""
    session_id = input_data.get("session_id", "") or ""
    state = read_state(session_id) if session_id else None
    own = [m for m in ((state or {}).get("modified_files") or []) if (m or {}).get("session_id", session_id) == session_id]
    unknown = [u for u in ((state or {}).get("unknown_writes") or []) if (u or {}).get("session_id", session_id) == session_id]
    _code_gate_no_ask(input_data, config, state, session_id, own, input_data.get("transcript_path") or "",
                      i18n.t("hook.stop.who_judge"), {"event": "judge_block"}, unknown)


def _declared_once(state: Dict[str, Any], key: str, session_id: str, since: int, text: str) -> bool:
    """收尾宣告留帳的去重：同一個使用者回合同一句只記一筆（被擋後重寫同一句不重記）。沒記過 → 記進 state[key] 並回 True。"""
    seen = state.setdefault(key, {})
    mine = seen.get(session_id) if isinstance(seen.get(session_id), dict) else {}
    said = list(mine.get("reasons") or []) if mine.get("turn") == since else []
    if text in said:
        return False
    seen[session_id] = {"turn": since, "reasons": [*said, text]}
    return True


def _mods_sig(mods: List[Dict[str, Any]]) -> str:
    """改檔情境指紋：本 session 改過的檔與各自最後一次修改的序號／時間（放行之後又改了檔就不一樣）。"""
    rows = sorted(f"{m.get('path')}|{m.get('last_seq', m.get('seq'))}|{m.get('last_at', m.get('at'))}"
                  for m in mods if isinstance(m, dict))
    return hashlib.sha1("\n".join(rows).encode("utf-8")).hexdigest()[:16]


def _wrote_code(mods: List[Dict[str, Any]], config: Dict[str, Any]) -> bool:
    """本 session 改過的檔裡有沒有程式檔（wg_code_path.is_code_path）。"""
    from wg_code_path import is_code_path
    return any(isinstance(m, dict) and is_code_path(str(m.get("path") or ""), config) for m in mods)


def handle_stop(input_data: Dict[str, Any], config: Dict[str, Any]) -> None:
    """Stop 收尾的目錄：照閘序一步一步跑。回 True 的步驟已經送了出口（擋、放行或中止通知），這次收尾到此為止；
    _timed_out(段名) 是計時段的打點：累計過 hook 上限的 80% → 寫回 state、放行，後面的步驟這次不跑。
    步驟之間共用的材料與記號都在 run（_StopRun），由前面幾步填好、後面的閘讀。"""
    _CHAIN_TAIL.clear()
    run = _open_run(input_data, config)
    if run is None:
        output_nothing()
        return
    if not run.guard:
        _memory_only_stop(run)
        return
    _timed_out = run._timed_out
    # ── 收尾材料（這幾步不擋）──
    _read_transcript(run)
    _close_turn_books(run)
    if _timed_out(i18n.t("hook.stop.lap_read_transcript")):
        return
    _collect_leftovers(run)
    if _timed_out(i18n.t("hook.stop.lap_leftover_ledger")):
        return
    if run.v6:
        _record_prompt_judgments(run.state, run.session_id, run.transcript, run.transcript_text)   # UPS 問過的「是不是否決」收宣告行（G6）
    if _timed_out(i18n.t("hook.stop.lap_correction_declarations")):
        return
    _prepare_block_extras(run)
    if _timed_out(i18n.t("hook.stop.lap_token_warning")):
        return
    _shared_facts(run)
    _log_equivalence_not_applicable(run)
    # ── 閘序（檔頭 1～5）：正確性閘在前，再來退避、收割、收尾檢核、同步，最後底部一般同步擋 ──
    if _chain_breaker(run):
        return
    if _red_test_gate(run) or _timed_out(i18n.t("hook.stop.lap_red_test_gate")):
        return
    if _reconcile_gate(run):
        return
    if _wait_for_background(run):
        return
    run.ev_now = _flag_evasion(run)
    if _timed_out(i18n.t("hook.stop.lap_code_reconcile")):
        return
    if _equivalence_proof_gate(run) or _timed_out(i18n.t("hook.stop.lap_equivalence")):
        return
    if _deferral_gate(run) or _timed_out(i18n.t("hook.stop.lap_deferral")):
        return
    if _harvest_gate(run) or _timed_out(i18n.t("hook.stop.lap_harvest")):
        return
    if _harvest_pending_gate(run) or _timed_out(i18n.t("hook.stop.lap_harvest_reconcile")):
        return
    if _scan_report_gate(run) or _timed_out(i18n.t("hook.stop.lap_closing_check")):
        return
    if _aec_pending_gate(run) or _timed_out(i18n.t("hook.stop.lap_report_pending")):
        return
    _notice_unseen_report(run)
    if _timed_out(i18n.t("hook.stop.lap_report_window")):
        return
    if _reload_advice_gate(run) or _timed_out(i18n.t("hook.stop.lap_reload_advice")):
        return
    if _sync_reminder_gate(run) or _timed_out(i18n.t("hook.stop.lap_sync_reminder")):
        return
    if _atom_audit_gate(run) or _timed_out(i18n.t("hook.stop.lap_consumption_audit")):
        return
    if _deep_postmortem_gate(run) or _timed_out(i18n.t("hook.stop.lap_deep_postmortem")):
        return
    _attribute_outcome(run)
    if _timed_out(i18n.t("hook.stop.lap_outcome_attribution")):
        return
    if _anti_loop_exit(run):
        return
    _sync_block_gate(run)


def _memory_only_stop(run: "_StopRun") -> None:
    """守門家族關（插件版）的收尾：只跑記憶本體——回收讀過的檔、知識收割兩閘（同 session 第偶數次判欠才擋，wg_harvest.harvest_send_mode）、
    卡片取用稽核、效用歸因、萃取 worker；紅測、寫碼查核、等值、退縮、收尾檢核、同步、深度檢討、連鎖斷路器一道都不跑、模組也不載。
    擋或放行的出口與守門開時同一套（output_block／output_nothing）；超時降級照分段打點。"""
    _timed_out = run._timed_out
    _read_transcript(run)
    _close_turn_books(run)
    if _timed_out(i18n.t("hook.stop.lap_read_transcript")):
        return
    _prepare_block_extras(run)
    if _timed_out(i18n.t("hook.stop.lap_token_warning")):
        return
    _shared_facts(run)
    if _wait_for_background(run):
        return
    if _harvest_gate(run) or _timed_out(i18n.t("hook.stop.lap_harvest")):
        return
    if _harvest_pending_gate(run) or _timed_out(i18n.t("hook.stop.lap_harvest_reconcile")):
        return
    if _atom_audit_gate(run) or _timed_out(i18n.t("hook.stop.lap_consumption_audit")):
        return
    _attribute_outcome(run)
    if _timed_out(i18n.t("hook.stop.lap_outcome_attribution")):
        return
    if _anti_loop_exit(run):
        return
    _maybe_spawn_user_extract_worker(run.session_id, run.state, run.config)
    output_nothing()


@dataclass
class _StopRun:
    """一次 Stop 收尾從頭到尾共用的材料與記號：欄位照「誰填」分組，之後的步驟只讀，標「會改」的除外。
    每個步驟開頭把要用的欄位取成區域變數（名字與規則文字、源碼檢核的案例對得上），本體只用區域變數。"""
    # 開場（_open_run）
    input_data: Dict[str, Any]
    config: Dict[str, Any]
    session_id: str
    state: Dict[str, Any]
    max_blocks: Any            # 共用擋次上限 stop_gate_max_blocks（設定原值）
    v6: bool                   # 寫碼守門開關；關＝只跑 9e476fd0 就有的閘
    guard: bool                # 守門家族開關（guard_family_on）；關＝插件版，只跑知識收割、取用稽核、效用歸因（_memory_only_stop）
    turn_seq: int
    since: Any                 # 本回合起點：使用者打字開的回合（規則 TURN_SINCE_USER_PROMPT）
    stop_count: int            # 開場時的共用擋次；各閘擋下時記 stop_count + 1
    phase: Any                 # 開場時的 phase
    # 讀 transcript（_read_transcript）
    failing: Any = None
    cwd: Any = ""
    transcript: Optional[Path] = None
    transcript_text: Any = ""
    last_text: Any = ""
    code_text: Any = ""
    turn_claim: Any = ""
    # 擋訊息附件（_prepare_block_extras）
    token_warn: Any = ""
    accept_hint: Any = ""
    # 各閘共用前置（_shared_facts）
    own_mod_files: List[Dict[str, Any]] = field(default_factory=list)   # 會改：底部同步擋換成剔掉已同步的那份，預覽也跟著看
    coding: Any = False
    committed_this_turn: Any = False
    aec: Dict[str, Any] = field(default_factory=dict)
    emitted_this_turn: Any = False
    aec_covers_sync: Any = False
    aec_up_to_date: Any = False
    hv_mode: Any = None
    # 跨步驟的記號（會改）
    chain_record_only: bool = False                  # 連鎖斷路器警報後只記錄：_chain_breaker 給起始值，每次 _chain_hold 更新
    unsynced: Optional[set] = None                   # 預覽用的「還沒同步的檔」：一次收尾只問一次 git（_later_gates）
    ev_now: Optional[Dict[str, Any]] = None          # 這次收尾新記的退避旗標（_flag_evasion 回的；退縮閘擋下同一句時拿掉）

    @property
    def tpath(self) -> Optional[str]:
        """transcript 路徑字串（放行題、擋的位置用）；沒有 transcript 是 None。"""
        return str(self.transcript) if self.transcript else None

    def _done(self, text: str) -> bool:
        """收尾閘的「宣告完成」：這一則說了完成，或本回合有宣告完成的那一則（_read_transcript 找的 turn_claim）。"""
        _turn_claim = self.turn_claim
        return bool(text and claims_completion(text)) or bool(_turn_claim)

    def _timed_out(self, seg: str) -> bool:
        """這一段跑完打點（wg_hook_timing）；累計過 hook 上限的 80%（已留帳＋stderr）→ 寫回 state、放行，後面的閘這次不跑。"""
        session_id, state = self.session_id, self.state
        if not _hook_lap(seg):
            return False
        write_state(session_id, state)
        output_nothing()
        return True

    def _piggyback(self, reason: str, gate: Optional[str] = None, items: Optional[List[str]] = None) -> str:
        """附後面還會擋的閘／token 預警／驗收提示。gate 有給（對帳閘＝"reconcile"）就記這個閘最後一次擋的位置；
        那條路是先 write_state 再呼叫本函式，所以記完自己再寫一次 state，擋的位置才不會遺失。
        items＝這次列給使用者看的那幾件（沒給就照擋訊息取 release_items）：使用者之後放行只放這幾件。"""
        state, session_id, _tpath = self.state, self.session_id, self.tpath
        _token_warn, _accept_hint = self.token_warn, self.accept_hint
        if gate:
            _rel.mark_block(state, session_id, gate, _tpath, int(state.get("turn_seq", 0)),
                            items if items is not None else _rel.release_items(reason))
        if self.v6 and self.guard:   # 寫碼守門開才預覽後面還會擋的閘（規則 BLOCK_LISTS_LATER_GATES）；守門家族關沒有後面的閘
            try:
                note = self._later_gates(reason)
            except Exception as e:  # noqa: BLE001
                note = ""
                print(i18n.t("hook.stop.also_owed_preview_skipped", error=f"{type(e).__name__}: {e}"), file=sys.stderr)
            if note:
                reason = f"{reason}\n{note}"
        if _token_warn:
            state["token_warn_emitted"] = True
            reason = reason + _token_warn
        if _accept_hint:
            state["acceptance_hint_emitted"] = True
            reason = reason + _accept_hint
        if gate:
            write_state(session_id, state)
        return reason

    def _later_gates(self, reason: str, names: Optional[List[str]] = None) -> str:
        """規則 BLOCK_LISTS_LATER_GATES：這次擋下時，照目前狀態依閘序預覽後面還會擋的閘（知識收割、
        收尾檢核、同步），讓模型一回合
        補齊，不用每道閘各多花一回合。只讀不寫：不動 state、不吃預算；後面的閘還有每回合一次、擋滿放行等條件，以真正擋下為準。
        names 有給就補上預覽裡的閘名（連鎖斷路器用：預覽裡的閘之後擋下，不算這道閘之後的動作造成的）。"""
        state, config, session_id, turn_seq, v6, phase = self.state, self.config, self.session_id, self.turn_seq, self.v6, self.phase
        last_text, own_mod_files, _done, _hv_mode = self.last_text, self.own_mod_files, self._done, self.hv_mode
        committed_this_turn, emitted_this_turn, aec_covers_sync = self.committed_this_turn, self.emitted_this_turn, self.aec_covers_sync
        later: List[Tuple[Tuple[str, ...], str]] = []
        if "[Guardian:KnowledgeHarvest]" not in reason and harvest_gate_reason(
                state, session_id, last_text, config, own_mod_files, turn_seq, gate_turn=_user_turn(state, turn_seq),
                claimed=_done(last_text)):
            later.append((("KnowledgeHarvest",), i18n.t("hook.stop.owed_harvest"), "HARVEST_AT_PHASE_DONE"))
        if ("[Guardian:ScanReport]" not in reason and own_mod_files and not state.get("scan_report_warned")
                and not committed_this_turn and _scan_report_gate_on(config)
                and detect_missing_aec_emission(last_text, own_mod_files, state.get("recent_user_prompts", []) or [],
                                                int(config.get("min_files_to_block", 2)), emitted_this_turn,
                                                claimed=_done(last_text))):
            later.append((("ScanReport",), i18n.t("hook.stop.owed_closing_check", fix=CLOSING_CHECK_FIX), "CLOSING_CHECK_REQUIRED"))
        if ("[Guardian:SyncReminder]" not in reason and "[Workflow Guardian]" not in reason
                and not state.get("muted") and phase not in ("done", "syncing")):
            sr = config.get("sync_reminder", {}) or {}
            if self.unsynced is None:    # 一次收尾只問一次 git（連鎖斷路器的閘名預覽與擋訊息的預覽共用）
                self.unsynced = {(m or {}).get("path") for m in _drop_synced(own_mod_files)} - {None, ""}
            unsynced = self.unsynced
            # 收尾檢核 (g) 已交代、或後面還欠收尾檢核（(g) 會交代版控）只免同步提醒那段（SYNC_COVERED_BY_CLOSING_CHECK）；
            # 底部一般擋（待記知識、文件漂移）不看收尾檢核，照它自己的判定列
            reminder_owed = (unsynced and not aec_covers_sync and not any("ScanReport" in n for n, *_ in later)
                             and sr.get("enabled", True)
                             and int(state.get("sync_reminder_count", 0)) < int(sr.get("max_reminders", 1)))
            block_items = _sync_block_items(state, len(unsynced), int(config.get("min_files_to_block", 2)),
                                            _user_turn(state, turn_seq))
            if reminder_owed:
                later.append((("SyncReminder", "SyncBlock"),
                              i18n.t("hook.stop.owed_sync_unsynced", count=len(unsynced)), "SYNC_REMINDER_UNSYNCED"))
            elif block_items:
                later.append((("SyncBlock",), i18n.t("hook.stop.owed_sync_block", count=len(block_items)),
                              "SYNC_BLOCK_PENDING_ITEMS"))
        # 判成提醒的閘之後不會擋，不列進預覽（GATE_MODE_TABLE）；收割看這次是第幾次欠
        later = [e for e in later if (_hv_mode if e[2] == "HARVEST_AT_PHASE_DONE" else _gate_mode(e[2], v6)) == _BLOCK]
        if names is not None:
            names.extend(g for n, _, _ in later for g in n)
        if not later:
            return ""
        return i18n.t("hook.stop.also_owed", items=i18n.t("hook.stop.evidence_sep").join(t for _, t, _ in later))

    def _chain_hold(self, gate: str, what: str) -> bool:
        """這道閘要擋或記欠了：記一筆觸發（連預覽裡的閘名，規則 CHAIN_EDGE_PREVIEWED；只取閘名，擋訊息的預覽由 _piggyback 照原樣算）。
        True＝只記錄，呼叫端跳過擋與記欠。"""
        state, config, session_id, input_data, v6, _since = self.state, self.config, self.session_id, self.input_data, self.v6, self.since
        if not (v6 and self.guard):
            return False
        names: List[str] = []
        if not self.chain_record_only:     # 已在只記錄就不預覽閘名
            tag = "[Workflow Guardian]" if gate == "SyncBlock" else f"[Guardian:{gate}]"
            try:
                self._later_gates(f"{tag} {what}", names)       # 帶上這道閘的標記：預覽排除自己的方式跟真的擋訊息一樣
            except Exception as e:  # noqa: BLE001  預覽壞了只少一條排除規則，照記觸發
                names.clear()
                print(i18n.t("hook.stop.also_owed_preview_failed", error=f"{type(e).__name__}: {e}"), file=sys.stderr)
        self.chain_record_only = _chain_gate(session_id, _since, gate, what, input_data, config, state, later=names)
        return self.chain_record_only


def _mod_turn(m: Dict[str, Any]) -> int:
    """改檔紀錄最後改到的回合；沒有回合號（legacy entry）回 0。"""
    return int((m or {}).get("last_turn_seq") or (m or {}).get("turn_seq") or 0)


def _open_run(input_data: Dict[str, Any], config: Dict[str, Any]) -> Optional[_StopRun]:
    """開場：讀 state、定本回合、共用擋次照寫碼守門開關計次。讀不到 state 回 None（呼叫端放行）。"""
    session_id = input_data.get("session_id", "")
    state = _ensure_state(session_id, input_data, config)
    if not state:
        return None

    max_blocks = config.get("stop_gate_max_blocks", 2)
    v6 = v6_enabled(config)     # 關＝只跑 9e476fd0 就有的閘（各寫碼守門閘的分派點各自看它）
    # 「本回合」照規則 TURN_SINCE_USER_PROMPT：使用者打字開的回合起到現在（同伴訊息、背景通知不另開回合）。
    turn_seq = int(state.get("turn_seq", 0))
    _since = _user_turn(state, turn_seq)
    # 共用擋次（stop_gate_max_blocks 那份）：寫碼守門開＝每個使用者回合重新計次（同一回合擋滿後放行，換了使用者回合歸零）；
    # 寫碼守門關＝照 9e476fd0，整個 session 累計
    if v6 and state.get("stop_blocked_turn") != _since:
        state["stop_blocked_count"], state["stop_blocked_turn"] = 0, _since
    stop_count = int(state.get("stop_blocked_count", 0) or 0)
    phase = state.get("phase", "working")
    return _StopRun(input_data=input_data, config=config, session_id=session_id, state=state, max_blocks=max_blocks,
                    v6=v6, guard=guard_family_on(config), turn_seq=turn_seq, since=_since, stop_count=stop_count, phase=phase)


def _read_transcript(run: _StopRun) -> None:
    """讀 transcript（填 run 的「讀 transcript」那組）：本 session 的紅測條目、cwd、transcript 與尾段、最後一則、
    寫碼查核看的真正最後一句、本回合宣告完成的那一則。"""
    state, session_id, input_data, v6 = run.state, run.session_id, run.input_data, run.v6
    # 只認本 session 自己的條目（共用／merged state 下隔壁 session 的紅測不擋本 session；沒蓋 sid 的舊條目視為他人）
    failing = _own_failing_tests(state, session_id)
    cwd = state.get("session", {}).get("cwd", "") or input_data.get("cwd", "")
    # transcript 先用 hook 輸入給的路徑：session 中途切進 worktree（cwd 換了）之後，transcript 寫在新 cwd 的資料夾，
    # 拿 state 裡開場時的 cwd 推會找不到 → 擋的位置記成 0、讀不到 AskUserQuestion 的放行答案。輸入沒給或檔不在才自己推。
    _tp_in = str(input_data.get("transcript_path") or "")
    transcript = (Path(_tp_in) if _tp_in and Path(_tp_in).is_file()
                  else _find_session_transcript(session_id, cwd) if cwd else None)
    # 單次 tail-read：本函式所有 transcript 消費者（last_text / token 預警 /
    # usefulness turn 文字）共用同一份尾段，取代先前各自全檔讀取。
    transcript_text = read_transcript_tail(transcript)
    last_text = get_last_assistant_text(transcript, text=transcript_text)          # 退避偵測等：只看 >30 字的段落
    code_text = (_code_check_text(input_data, transcript, transcript_text) if run.guard   # 寫碼查核：真正的最後一句
                 else last_text)
    # 收尾閘的「宣告完成」看整回合：被別的閘擋下後補件那則常不重說完成（wg_evasion.turn_completion_text 找出本回合宣告完成的
    # 那一則；最後一則有談到完成與否就照它）。內容檢查（退避詞、宣稱詞）仍看最後一則
    _turn_claim = ""
    if v6:
        from wg_evasion import turn_completion_text
        _turn_claim = turn_completion_text(transcript_text)
    run.failing, run.cwd, run.transcript, run.transcript_text = failing, cwd, transcript, transcript_text
    run.last_text, run.code_text, run.turn_claim = last_text, code_text, _turn_claim


def _close_turn_books(run: _StopRun) -> None:
    """收這回合的帳：關回合、記版控口令的 push（寫碼守門開）、回收 accessed_files；有變就先寫 state。"""
    state, session_id, v6, transcript, transcript_text = run.state, run.session_id, run.v6, run.transcript, run.transcript_text
    # accessed_files 回收（取代 per-Read PostToolUse hook）——先寫進 state，
    # 後續任何 gate 的 write_state 也會一併固化；此處立即寫防走到不寫 state 的路徑。
    # 關回合：下一則 UserPromptSubmit 才是新回合的第一句（turn_prompts 重開，見 ups_gates.track_turn_prompts）。
    # 寫碼守門開：版控口令用到這批 push 完成為止，上次收尾之後跑成功的 push 記下來（ups_gates.order_spent 據此判口令用掉了）；
    # 寫碼守門關照 9e476fd0，口令 commit 過就用掉，不掃
    turn_was_open = bool(state.get("turn_open"))
    state["turn_open"] = False
    _pushed = False
    if v6 and run.guard:
        from handlers.ups_gates import note_order_push
        if transcript:
            state["order_transcript"] = str(transcript)
        _pushed = note_order_push(state, transcript)
    if _harvest_accessed_files(state, transcript_text) or turn_was_open or _pushed:
        write_state(session_id, state)


def _collect_leftovers(run: _StopRun) -> None:
    """殘檔帳本：掃 session scratchpad、比開場快照進帳（fail-open）。"""
    state, session_id, cwd, v6 = run.state, run.session_id, run.cwd, run.v6
    # 殘檔帳本：每次 Stop 掃一次 session scratchpad、比一次開場快照（SessionStart 記的工作樹與分支）進帳
    # （模型沒 emit 報告也不漏；python 腳本裡開的工作樹指令解析看不到，靠快照補）。fail-open。
    try:
        aec_ledger.collect_at_completion(session_id, cwd, None, int(state.get("turn_seq", 0)),
                                         snapshot=state.get("git_snapshot") if v6 else None)
    except Exception as e:  # noqa: BLE001
        print(i18n.t("hook.stop.cleanup_failed", error=f"{type(e).__name__}: {e}"), file=sys.stderr)


def _prepare_block_extras(run: _StopRun) -> None:
    """擋訊息附件（填 run.token_warn、run.accept_hint）：各閘擋下時由 _piggyback 接在擋訊息尾巴。"""
    state, config, session_id, transcript, transcript_text = run.state, run.config, run.session_id, run.transcript, run.transcript_text
    # ── token 預警 proxy（piggyback 既有 block，不獨立打斷）──────
    # 早段算一次預警句（純函式、無副作用）；於下方各 gate 將 output_block(reason)
    # 前用 _piggyback append。token_warn_emitted 旗標只在「實際 append」時才標，
    # 並隨該 gate 的 write_state 固化（一次性、整 session 一次）。要附什麼由插件決定（token_warn_payload）。
    _token_warn = token_warn_payload(
        state, config, transcript, transcript_text=transcript_text
    )
    # 迴歸累積提示同為 piggyback payload：驗收裁判真命中 → 建議補測試/落 atom。
    _accept_hint = _acceptance_regression_hint(state, session_id, config) if run.guard else ""
    run.token_warn, run.accept_hint = _token_warn, _accept_hint


def _shared_facts(run: _StopRun) -> None:
    """各閘共用前置（填 run 的「各閘共用前置」那組）：本 session 自己改的檔、是不是寫碼回合、本回合 commit 過沒、
    收尾檢核交了沒與還算不算數、這次判欠收割擋不擋。"""
    state, config, session_id, turn_seq, v6 = run.state, run.config, run.session_id, run.turn_seq, run.v6
    last_text, _since, _done = run.last_text, run.since, run._done
    mod_files_all = state.get("modified_files", []) or []
    # 只認「本 session 自己 Edit/Write 的檔」——共用工作樹/merged state 下，他 session
    # 改的 core 檔（session_id 不符）不得誤觸發本 session 的收尾檢核。未標記 session_id
    # 的 legacy entry 保守視為本 session（fail-open，不漏防退避）。
    own_mod_files = [
        m for m in mod_files_all
        if (m or {}).get("session_id", session_id) == session_id
    ]
    # 規則 CODING_TURN_ONLY：退避偵測、退縮閘、收尾檢核只在寫碼回合跑（收割、收割待補照舊每回合）；
    # 本 session 前幾回合改過程式、這則宣告完成也算（改碼已經發生過，收尾照查）
    _coding = v6 and run.guard and (coding_turn(state, config) or (_done(last_text) and _wrote_code(own_mod_files, config)))
    if not _coding:
        _hook_quiet()                                # 非寫碼回合不寫計時帳
    # 純 VCS commit turn 豁免收尾檢核：本 turn 已把工作寫進 VCS 歷史（可稽核＝與「藏」相反），
    # anti-evasion 目的在 commit 那刻消解。不開後門——豁免綁「本 turn 真的 commit 了」
    # （post_tool_use 記的 last_commit_turn_seq），而非「本 turn 沒 Edit」；光宣告完成不 commit
    # 仍被擋。未 commit 就 commit 的檔仍由 SyncReminder / 一般 block 兜底。「本回合」＝函式開頭算好的 _since 起到現在。
    committed_this_turn = bool(turn_seq) and in_turn(state.get("last_commit_turn_seq"), turn_seq, _since)
    # emit 滿足＝本回合有呼叫 anti_evasion_report。★雙鍵（回合 **且** session_id）為硬性：
    # merged/sibling session 共用同一實體 state 檔且共用同一 turn_seq 計數器，唯 session_id 能
    # 區辨——否則隔壁 session 的 emit 會誤放行本 session（重演 own_mod_files 要防的洩漏）。
    # bool(turn_seq) 護欄：防 turn_seq==0 的 fallback state 以 0==0 假滿足。
    aec = state.get("anti_evasion_report") or {}
    emitted_this_turn = (
        bool(turn_seq)
        and in_turn(aec.get("turn_seq"), turn_seq, _since)
        and aec.get("session_id") == session_id
    )

    # 收尾檢核 (g) 交代的版控還算數：本回合交過、而且交了之後本 session 沒再改檔（報告沒記時間或改檔沒記時間 → 不算，照提醒）
    aec_covers_sync = emitted_this_turn and bool(aec.get("at")) and all(
        str((m or {}).get("last_at") or (m or {}).get("at") or "9") <= str(aec["at"]) for m in own_mod_files)
    # 上一份收尾檢核之後本 session 沒再 Edit/Write（每個改檔最後改到的回合都不晚於報告那回合）→ 沒有新東西可報，
    # ScanReport 視同已交，純問答回合不再逼重交整份（同回合的後續改動由 emitted_this_turn 覆蓋，這裡只看更早的回合）。
    # 改檔紀錄沒有回合號（legacy entry）就不知道是不是報告之後改的，不算已交（fail-safe）。
    aec_up_to_date = (
        bool(aec.get("turn_seq"))
        and aec.get("session_id") == session_id
        and all(0 < _mod_turn(m) <= int(aec.get("turn_seq") or 0) for m in own_mod_files)
    )

    # 這次判欠收割要擋還是只留帳（規則 HARVEST_AT_PHASE_DONE）：預覽與收割閘共用
    _hv_mode = harvest_send_mode(state, session_id, _gate_mode("HARVEST_AT_PHASE_DONE", v6))
    run.own_mod_files, run.coding, run.committed_this_turn, run.aec = own_mod_files, _coding, committed_this_turn, aec
    run.emitted_this_turn, run.aec_covers_sync, run.aec_up_to_date = emitted_this_turn, aec_covers_sync, aec_up_to_date
    run.hv_mode = _hv_mode


def _log_equivalence_not_applicable(run: _StopRun) -> None:
    """「等值證法｜不適用」的宣告留帳（寫碼守門開；排在所有收尾閘前面）。"""
    state, config, session_id, turn_seq, v6, cwd = run.state, run.config, run.session_id, run.turn_seq, run.v6, run.cwd
    code_text, own_mod_files, _since, _done = run.code_text, run.own_mod_files, run.since, run._done
    # 規則 EQUIVALENCE_NA_DECLARED_ALWAYS_LOGGED：收尾有「等值證法｜不適用｜<理由>」（理由成立）就留帳，排在所有收尾閘前面；
    # needed＝等值閘這回合管不管（意圖 refactor、宣告完成、改過程式檔、閘開著）。同回合同一行只記一筆。
    for _na in parse_equivalence_lines(code_text)["na"] if v6 else ():
        if reason_present(_na["reason"]) and _declared_once(state, "equivalence_na_declared", session_id, _since, _na["line"]):
            _eq_app = _equivalence_applies(state, config, code_text, own_mod_files, turn_seq, _since, cwd, _done(code_text))
            append_guard_log("equivalence", {"event": "not_applicable", "session_id": session_id, "turn_seq": turn_seq,
                                             "intent": _eq_app[0] if _eq_app else None, "line": _na["line"][:200],
                                             "reason": _na["reason"][:300], "needed": bool(_eq_app and _eq_app[0] == "refactor")})
            write_state(session_id, state)


def _chain_breaker(run: _StopRun) -> bool:
    """連鎖斷路器（寫碼守門開才有）：警報後還沒送經過表 → 擋這一次送表（回 True）；使用者答了三選一的結果附在這次收尾的出口。"""
    state, config, session_id, input_data, v6, _since = run.state, run.config, run.session_id, run.input_data, run.v6, run.since
    # ── 連鎖斷路器（判定 hooks/wg_chain.py，帳與還原 lib/chain_store.py；寫碼守門開著才有）─────────────
    # 各閘之前問一次：警報後還沒送經過表 → 擋這一次送表；使用者答了三選一 → 照答案做（還原／歸零／放行），結果附在這次收尾的出口。
    # 之後每道閘要擋或記欠之前問 _chain_hold：警報後只記錄（跳過那道閘的擋與記欠、往下走），這一次讓警報響就送中止通知取代原擋訊息。
    if v6:
        _cs_out = chain_store.on_stop(session_id, _since, input_data, config)
        if _cs_out is not None and _cs_out.block:
            write_state(session_id, state)
            output_block(_cs_out.text)
            return True
        if _cs_out is not None:
            _CHAIN_TAIL.append(_cs_out.text)
            print(_cs_out.text, file=sys.stderr)
        run.chain_record_only = chain_store.record_only(session_id, _since, input_data, config)
    return False


def _red_test_gate(run: _StopRun) -> bool:
    """紅測閘：本 session 自己的紅測還在、最後一則宣告完成 → 擋（回 True）。"""
    state, session_id, failing, last_text, stop_count = run.state, run.session_id, run.failing, run.last_text, run.stop_count
    _piggyback = run._piggyback
    # 紅測閘（寫碼守門開或關同一套）：本 session 自己的紅測還在、最後一則宣告完成 → 擋。使用者說「先這樣／跳過」時
    # 紅測清單由 UserPromptSubmit 整份清掉（handlers/ups_gates）
    if failing and claims_completion(last_text):
        state["stop_blocked_count"] = stop_count + 1
        reason = _piggyback(testfail_message(failing))
        write_state(session_id, state)
        output_block(reason)
        return True
    return False


def _reconcile_gate(run: _StopRun) -> bool:
    """寫碼查核（Reconcile Gate，收尾宣稱 vs 驗證收據，寫碼守門開）：擋了回 True。獨立預算、擋滿問使用者、放行與無法互動的放行。"""
    state, config, session_id, turn_seq, v6, input_data = run.state, run.config, run.session_id, run.turn_seq, run.v6, run.input_data
    code_text, transcript, transcript_text, _turn_claim, _since, _tpath = (
        run.code_text, run.transcript, run.transcript_text, run.turn_claim, run.since, run.tpath)
    own_mod_files, aec, emitted_this_turn = run.own_mod_files, run.aec, run.emitted_this_turn
    _chain_hold, _piggyback = run._chain_hold, run._piggyback
    # ── Reconcile Gate（收尾宣稱 vs 驗證收據）──────────
    # 緊接 TestFailGate 之後、evasion 偵測之前（correctness 閘優先）。不吃共用 stop_gate_max_blocks：
    # 獨立預算 reconcile_gate.max_blocks_per_turn（state reconcile_gate_turn[sid]={turn,count}）；預算與
    # 放行的「回合」是使用者本人打字開的回合（_user_turn：同伴訊息、背景通知、hook 回饋續跑都不開新回合、不重置次數）。
    # 寫碼查核（對帳 (a)(b)(c)(v)）擋滿後不放行：記 pending_release、改送擋滿訊息要模型問使用者；每次擋（不等擋滿）都查
    # 使用者有沒有在最後一次擋之後放行（wg_release.user_released）→ 放這一回合、清 pending 與計數、guard-reconcile.jsonl
    # 記 user_release（健檢列 red）；擋之前的舊答案不算。放行記「那幾件」（release_items）：之後的回合同一件原因還在就不再擋、
    # 不再問，原因消失就從放行紀錄拿掉；新出現的照判。
    # pending 只在同一個使用者回合有效（wg_release.pending_of）：換了使用者回合預算重給、不拿先前的宣稱重查。
    # 不看措辭的部分（(v) 搬程式目錄）每次 Stop 都判，last_text 空也判；(a)(b)(c) 對宣稱，同一回合的 pending 帶著第一次擋滿時的
    # 宣稱原話（claims，之後的文字不附加也擠不掉），換一句話也拿原話重查。pending 只在兩種情況清：重新查核後原因真的消失，
    # 或使用者合格放行：「沒有執行查核」不等於「查核通過」。沒帶 claims 的 pending → 原宣稱無從重查 → 照擋，只能使用者放行。
    # 無法互動（headless／SDK）：不叫它問，擋滿就放行並留帳 event=noninteractive_released（比照子代理）。
    # (e) 收尾報告欄屬記憶與報告類：照舊第 3 次放行並記 reconcile_overflow（健檢 red），不走放行詢問；與寫碼部分同時擋滿時分開列。
    _rg_cfg = reconcile_cfg(config)
    if not (v6 and _rg_cfg.get("enabled", True)):
        return False
    _pend = _rel.pending_of(state, session_id, "reconcile")
    _prev_claims = [str(c) for c in (_pend or {}).get("claims") or []]
    if _turn_claim and not claims_completion(code_text) and _turn_claim not in _prev_claims:
        _prev_claims.append(_turn_claim)     # 補件那則沒重說完成：(a)(b)(c) 與交付邊界照本回合宣告完成的那一則判
    _jeb_prev = json.loads(json.dumps((state.get("judge_error_blocked") or {}).get(session_id)))   # GATE_SELF_ERROR 擋過的錯
    _vg_reminds: List[Tuple[str, str]] = []
    _code, _report, _vg_kind = _verify_gates(
        state, session_id, turn_seq, code_text, transcript, transcript_text, own_mod_files,
        _rg_cfg, aec, emitted_this_turn, claims=_prev_claims, pending=_pend is not None,
        reminds=_vg_reminds,
    )
    for _rule, _text in _vg_reminds:      # 提醒類條目（(b)(c)(v)(e)）：只留帳（STOP_REMIND_LEDGER_ONLY）
        _remind_logged("reconcile", "ReconcileGate", _rule, _text, session_id, turn_seq)
    if _pend is not None and "claims" not in _pend:
        _code = f"{_code}\n{LEGACY_PENDING}" if _code else LEGACY_PENDING
        _vg_kind = _vg_kind or "reconcile"
    _pend_cleared = False
    if _code is None and _pend:
        _rel.clear_pending(state, session_id, "reconcile")            # 重新查核過了（原因真的消失）→ 清 pending
        _pend, _pend_cleared = None, True
    _uturn = _since
    _kept: List[str] = []
    _ctx = _mods_sig(own_mod_files)          # 放行之後又改了檔＝情境變了，先前放行的那幾件不再沿用
    if _code and _rel.released_this_turn(state, session_id, "reconcile", _uturn):
        print(i18n.t("hook.stop.reconcile_released_turn"), file=sys.stderr)
        _code = None
    else:
        # 先前回合放行過的那幾件：原因還在照放行、拿出擋訊息；原因消失（這次沒有它）就從放行紀錄拿掉
        _kept = _rel.keep_released(state, session_id, "reconcile", _rel.release_items(_code) if _code else [], _ctx)
        if _kept:
            _code = _rel.drop_items(_code, _kept)
            print(i18n.t("hook.stop.reconcile_kept_released", count=len(_kept),
                         rest=i18n.t("hook.stop.reconcile_kept_released_rest") if _code else ""), file=sys.stderr)
    if not (_code or _report):
        if _pend_cleared or _kept:
            write_state(session_id, state)
        return False
    # 規則 BLOCK_BUDGET_SHARED：寫碼查核每個使用者回合共用一份預算（鍵 "*"，不分是哪一條規則），擋滿 max 才算擋滿；
    # _count＝本回合擋過幾次（log 沿用）
    _budget = state.setdefault("reconcile_gate_turn", {})
    _rec = _budget.get(session_id) if isinstance(_budget.get(session_id), dict) else None
    _max = int(_rg_cfg.get("max_blocks_per_turn", 2))
    _full_rules = budget_exhausted(_rec, _uturn, _max, ["*"] if _code else [])
    _count = int(_rec.get("count", 0)) if _rec and int(_rec.get("turn", -1)) == _uturn else 0
    _code_full = bool(_code) and (bool(_full_rules) or _pend is not None)
    if _full_rules:
        print(i18n.t("hook.stop.reconcile_budget_shared", max=_max), file=sys.stderr)
    # 每次擋都查放行（不等擋滿）：最後一次擋之後使用者在 AskUserQuestion 選了放行或打字放行 → 放這一回合。無法互動的不查。
    _noninteractive = non_interactive_reason(input_data, _tpath) if _code else None
    _how = user_released(state, session_id, "reconcile", _tpath) if _code and not _noninteractive else None
    if _how:
        _shown = _rel.shown_items(state, session_id, "reconcile")      # 只放使用者在擋訊息裡看過的那幾件
        _rel.record_release(state, session_id, "reconcile", _uturn,
                            [*_kept, *(i for i in _rel.release_items(_code) if _shown is None or i in _shown)], _ctx)
        _rel.mark_block(state, session_id, "reconcile", _tpath, turn_seq)   # 這個答案用掉了：之後要放行得再答一次
        _rel.clear_pending(state, session_id, "reconcile")
        _budget[session_id] = {"turn": _uturn, "count": 0}
        append_guard_log("reconcile", {
            "event": "user_release", "session_id": session_id, "turn_seq": turn_seq, "kind": _vg_kind,
            "via": _how, "count": _count, "reason": _code[:300],
        })
        print(i18n.t("hook.stop.reconcile_user_release", how=_how), file=sys.stderr)
        _code = None
    elif _code_full and _noninteractive:
        # 沒有人能回答放行詢問：比照子代理，擋滿就放行並留帳（主 session 之後的收尾照看這些改檔）
        append_guard_log("reconcile", {
            "event": "noninteractive_released", "session_id": session_id, "turn_seq": turn_seq, "kind": _vg_kind,
            "count": _count, "why": _noninteractive, "reason": _code[:300],
        })
        print(i18n.t("hook.stop.reconcile_noninteractive_released", reason=_noninteractive, max=_max), file=sys.stderr)
        _code = None
    if not _code:
        _code_full = False
    # 收尾報告那一份（(e)，記憶與報告類）只數自己的「e」：寫碼查核先擋過幾次不吃掉它的次數
    _report_full = bool(budget_exhausted(_rec, _uturn, _max, ["e"]))
    if not _code and _report and _report_full:
        # 只剩收尾報告那一份：第 3 次放行並記 reconcile_overflow（健檢 red）
        state.setdefault("reconcile_overflow", {})[session_id] = {
            "turn": turn_seq, "at": _now_iso(), "kind": "report", "reason": _report[:300],
        }
        append_guard_log("reconcile_overflow", {
            "session_id": session_id, "turn_seq": turn_seq, "kind": "report", "count": _count,
            "reason": _report[:300],
        })
        print(i18n.t("hook.stop.reconcile_report_overflow", max=_max), file=sys.stderr)
        write_state(session_id, state)
        return False
    if not _code and not (_report and not _report_full):
        write_state(session_id, state)
        return False
    if _chain_hold("ReconcileGate", _code or _report):
        _unmark(state.get("judge_error_blocked"), session_id, _jeb_prev)
        write_state(session_id, state)
        return False
    _budget[session_id] = budget_tick(_rec, _uturn, ["*"] if _code else ["e"])
    _gate = "ReconcileGate"
    if _code_full:
        _rel.set_pending(state, session_id, "reconcile", turn_seq, _now_iso(), _code,
                         [code_text] if claims_completion(code_text)
                         else ([_turn_claim] if _turn_claim else []))      # pending 已在就沿用原 claims
        _summary = exhausted_summary(_code)                          # 列全部命中條目，不只第一條
        _n = _count if _full_rules else None                       # 擋滿來自本回合先前記的 pending：不報次數
        _msg = i18n.t("hook.stop.non_interactive_with_original", message=_rel.exhausted_message(_gate, _n, _summary), original=_code)
        if _report:
            _msg += i18n.t("hook.stop.reconcile_report_extra", report=_report)
    else:
        _msg = "\n".join(x for x in (_code, _report) if x)
    if _code and not _noninteractive:
        _msg += f"\n{ASK_HINT}"
    append_guard_log("reconcile", {
        "session_id": session_id, "turn_seq": turn_seq, "kind": _vg_kind, "count": _count + 1,
        "excerpt": _msg.split("\n")[1][:160] if "\n" in _msg else _msg[:160],
        **({"event": "exhausted_block"} if _code_full else {}),
    })
    write_state(session_id, state)
    output_block(_piggyback(_msg, "reconcile"))
    return True


def _wait_for_background(run: _StopRun) -> bool:
    """等背景工作：還有背景任務或排程在跑 → 後面的閘延到背景回報那次收尾（寫 state、放行，回 True）。"""
    state, session_id, input_data, turn_seq = run.state, run.session_id, run.input_data, run.turn_seq
    # ── 等背景工作（規則 STOP_DEFERS_WHILE_BACKGROUND_RUNNING）─────────────
    # 紅測閘與寫碼查核看的是「說了完成」，上面照跑；回合只是在等背景子代理／Monitor／排程叫醒時，退避偵測、退縮歸屬、收割、收尾檢核、
    # 同步、深度檢討、效用歸因、底部同步擋都延到背景工作回報那次收尾（同一個背景項目連延 BG_DEFER_MAX 次之後照擋）
    if _bg_defer(state, input_data, session_id, turn_seq):
        write_state(session_id, state)
        output_nothing()
        return True
    return False


def _flag_evasion(run: _StopRun) -> Optional[Dict[str, Any]]:
    """退避偵測（軟糾正，不擋）：退避語記成退避旗標與事件；回這次新記的那一筆（沒記回 None）。"""
    state, config, session_id, v6, _coding, last_text = run.state, run.config, run.session_id, run.v6, run.coding, run.last_text
    transcript, transcript_text, own_mod_files = run.transcript, run.transcript_text, run.own_mod_files
    _ev_now: Optional[Dict[str, Any]] = None      # 這次收尾新記的退避旗標（退縮閘擋下同一句時拿掉，見下）
    if not state.get("evasion_flag") and (_coding or not v6):
        recent_prompts = state.get("recent_user_prompts", []) or []
        _nd_audit: List[Dict[str, Any]] = []
        ev = detect_evasion(last_text, recent_prompts, audit=_nd_audit)
        # 規則 NOT_DOING_DECLARED：退避語對到有理由的不做行（含「使用者說「…」」）→ 不標、留帳
        for _a in _nd_audit:
            append_guard_log("evasion_declared", {"session_id": session_id, "turn_seq": int(state.get("turn_seq", 0)),
                                                  "phrase": _a.get("phrase", ""), "rule": _a.get("rule", ""),
                                                  "why": str(_a.get("why", ""))[:200], "line": str(_a.get("line", ""))[:200]})
        if ev and v6:
            # 規則 EVASION_CONTEXT_EXEMPT：context 快滿、延後的事已落地、使用者沒叫現在做 → 這筆延後語不標紅不注入（照記錄）
            try:
                _ex = _evasion_exempt_verdict(state, session_id, ev, transcript, transcript_text, config, own_mod_files)
            except Exception as e:  # noqa: BLE001
                _ex = None
                print(i18n.t("hook.stop.evasion_exempt_failed", error=f"{type(e).__name__}: {e}"), file=sys.stderr)
            if _ex is not None and not _ex.owed:
                append_guard_log("evasion_exempt", {"session_id": session_id, "turn_seq": int(state.get("turn_seq", 0)),
                                                    "phrase": ev.get("phrase", ""), "why": _ex.why})
                ev = None
        if ev:
            ev["at"] = _now_iso()
            state["evasion_flag"] = _ev_now = ev
            # 證據暫存（evasion_flag 會被下輪 UPS 注入後清空；事件列表留存供
            # AEC (b) 欄 cross-check——hook 實測 vs 模型自評，不信自評）
            events = state.setdefault("evasion_events", [])
            events.append({
                "phrase": ev.get("phrase", ""),
                "turn_seq": int(state.get("turn_seq", 0)),
                "at": ev["at"],
            })
            state["evasion_events"] = events[-10:]
            append_guard_log("evasion", {
                "session_id": session_id,
                "phrase": ev.get("phrase", ""),
                "excerpt": (ev.get("context_excerpt", "") or "")[:120],
            })
            write_state(session_id, state)
    return _ev_now


def _equivalence_proof_gate(run: _StopRun) -> bool:
    """等值證法閘（G7，寫碼守門開）：擋了回 True；hook 內部錯不擋，浮 stderr 並留帳。"""
    state, config, session_id, input_data, turn_seq, v6, cwd = (
        run.state, run.config, run.session_id, run.input_data, run.turn_seq, run.v6, run.cwd)
    code_text, own_mod_files, _since, _tpath = run.code_text, run.own_mod_files, run.since, run.tpath
    _done, _chain_hold, _piggyback = run._done, run._chain_hold, run._piggyback
    # ── Equivalence Gate（G7）──
    # 重構／效能意圖、本回合改過程式檔、宣告完成 → 等值證法三類與熱路徑一問的收尾核對（_equivalence_gate；規則在 wg_release_rules）。
    # 自己的預算（不吃 stop_gate_max_blocks），擋滿問使用者。判不出（hook 內部錯）→ 不擋，浮 stderr 並留帳。
    _eq_reminds: List[Tuple[str, str]] = []
    from handlers.stop_equivalence import EQ_GATE
    try:
        _eq_block = v6 and _equivalence_gate(state, session_id, config, input_data, code_text, own_mod_files, turn_seq, _since, _tpath, cwd,
                                      claimed=_done(code_text), hold=_chain_hold, reminds=_eq_reminds)
    except Exception as e:  # noqa: BLE001
        _eq_block = None
        print(i18n.t("hook.stop.equivalence_check_failed", gate=EQ_GATE, error=f"{type(e).__name__}: {e}"), file=sys.stderr)
        append_guard_log("equivalence", {"event": "error", "session_id": session_id, "turn_seq": turn_seq, "error": repr(e)[:200]})
    for _rule, _text in _eq_reminds:          # 提醒類條目只留帳（STOP_REMIND_LEDGER_ONLY）
        _remind_logged("equivalence", EQ_GATE, _rule, _text, session_id, turn_seq)
    if _eq_block:
        reason = _piggyback(_eq_block)
        write_state(session_id, state)          # 連同 _piggyback 標的一次性旗標一起寫
        output_block(reason)
        return True
    return False


def _deferral_gate(run: _StopRun) -> bool:
    """退縮歸屬（Deferral Gate）：主任務完工、context 未滿，收尾把可做的事推出去 → 擋（回 True）或只留帳。"""
    state, config, session_id, turn_seq, v6, cwd = run.state, run.config, run.session_id, run.turn_seq, run.v6, run.cwd
    stop_count, max_blocks, last_text, _coding, _since = run.stop_count, run.max_blocks, run.last_text, run.coding, run.since
    transcript, transcript_text, committed_this_turn, _ev_now = run.transcript, run.transcript_text, run.committed_this_turn, run.ev_now
    _done, _chain_hold, _piggyback = run._done, run._chain_hold, run._piggyback
    # ── Deferral Gate（退縮歸屬）────────────────────────────────
    # 主任務已完工（完成宣告 ∨ 本 turn 已 commit）且 context 用量 ≤ deferral_gate.
    # max_context_ratio 時，收尾把「帶受詞、可做的事」推給下個 session／獨立議題／
    # 非我造成 → 擋回三選一。每回合一次、共用 stop_gate_max_blocks
    # 全域預算。判定純函式在 wg_evasion.deferral_gate_reason；量測失敗 fail-open。
    # 底下四個「每回合最多擋一次」的閘（Deferral、KnowledgeHarvest、Harvest-Pending、AEC-Pending）的回合都用
    # _user_turn（使用者打字開的回合）：同伴訊息、背景通知每則都讓 turn_seq +1，拿它當鍵會每則訊息後又擋一次。
    _uturn = _since
    if stop_count < max_blocks and last_text and (_coding or not v6):
        _ah = config.get("auto_handoff", {}) or {}
        try:
            _dg_ratio = estimate_context_usage(
                transcript,
                _ah.get("context_window_tokens", 1_000_000),
                _ah.get("context_base_overhead_tokens", 15000),
                text=transcript_text,
            )
        except Exception:
            _dg_ratio = 0.0
        _dg_audit: List[Dict[str, Any]] = []
        try:
            dg = deferral_gate_reason(
                last_text,
                state.get("recent_user_prompts", []) or [],
                claimed=_done(last_text),
                turn_seq=_uturn,
                gated_turn=state.get("deferral_gate_turn"),
                committed_this_turn=committed_this_turn,
                context_ratio=_dg_ratio,
                config=config,
                audit=_dg_audit,
            )
        except Exception as e:
            dg = None
            print(f"[Guardian:DeferralGate] error (fail-open): {e}", file=sys.stderr)
        for _a in _dg_audit:   # 規則 NOT_DOING_DECLARED：退縮句對到不做行或追蹤行 → 不擋、留帳
            append_guard_log("deferral_declared", {"session_id": session_id, "turn_seq": turn_seq,
                                                   "phrase": _a.get("phrase", ""), "rule": _a.get("rule", ""),
                                                   "why": str(_a.get("why", ""))[:200], "line": str(_a.get("line", ""))[:200]})
        _record_tracking_lines(last_text, session_id, turn_seq, cwd)
        if dg and _gate_mode("DEFERRAL_OF_DOABLE_WORK", v6) == _REMIND:
            state["deferral_gate_turn"] = _uturn          # 一回合一次照記；不吃共用擋次、退避旗標照留給下一則注入
            _remind_logged("deferral", "DeferralGate", "DEFERRAL_OF_DOABLE_WORK", dg, session_id, turn_seq,
                           context_ratio=round(float(_dg_ratio), 3))
            write_state(session_id, state)
        elif dg and not _chain_hold("DeferralGate", dg):
            state["deferral_gate_turn"] = _uturn
            state["stop_blocked_count"] = stop_count + 1
            if v6 and _ev_now is not None and state.get("evasion_flag") is _ev_now and _deferral_covers(dg, _ev_now):
                # 退縮閘這次擋下的就是退避旗標那一句：已逼著當場處理，下一則使用者訊息不再追問同一件（事件照留給收尾檢核對照）
                state["evasion_flag"] = None
                append_guard_log("evasion", {"session_id": session_id, "event": "covered_by_deferral",
                                             "phrase": _ev_now.get("phrase", "")})
            append_guard_log("deferral", {
                "session_id": session_id,
                "turn_seq": turn_seq,
                "context_ratio": round(float(_dg_ratio), 3),
                "excerpt": dg.split("\n")[1][:160] if "\n" in dg else "",
            })
            write_state(session_id, state)
            output_block(_piggyback(dg))
            return True
    return False


def _harvest_gate(run: _StopRun) -> bool:
    """知識收割（KnowledgeHarvest Gate）：宣告完成＝階段完工 → 要求 knowledge_harvest_report；擋了回 True。"""
    state, config, session_id, turn_seq, v6 = run.state, run.config, run.session_id, run.turn_seq, run.v6
    last_text, own_mod_files, _uturn, _hv_mode = run.last_text, run.own_mod_files, run.since, run.hv_mode
    _done, _chain_hold, _piggyback = run._done, run._chain_hold, run._piggyback
    # ── KnowledgeHarvest Gate（階段完工知識收割）────────────────
    # 宣告完成＝階段完工 → 要求呼叫 knowledge_harvest_report（items=[] 也要）。判定在
    # wg_harvest.harvest_gate_reason（活動門檻、冷卻只認 validated、dismiss 詞表重用 wg_evasion）。
    # 不吃 stop_gate_max_blocks 共用預算、每回合最多擋一次（harvest_gate_turn[sid] 記 _user_turn）；
    # state 全按 session_id 分區——隔壁 session 的收割不放行本 session。
    # 寫碼守門開時同 session 每兩次判欠擋一次（_hv_mode）：第奇數次只留帳、第偶數次走擋路；連鎖斷路器攔下的不記次。寫碼守門關每次擋、不數。
    _hv_waived: List[Dict[str, str]] = []
    hv_reason = harvest_gate_reason(state, session_id, last_text, config, own_mod_files, turn_seq, gate_turn=_uturn,
                                    claimed=_done(last_text), waived=_hv_waived)
    _log_waivers(state, session_id, turn_seq, _uturn, "harvest", _hv_waived)   # 使用者說先這樣／跳過 → 放行並留帳
    if hv_reason and _hv_mode == _REMIND:
        state.setdefault("harvest_gate_turn", {})[session_id] = _uturn
        _remind_logged("knowledge_harvest", "KnowledgeHarvest", "HARVEST_AT_PHASE_DONE", hv_reason, session_id, turn_seq,
                       own_mod_files=len(own_mod_files), owed_count=note_owed(state, session_id))
        write_state(session_id, state)
    elif hv_reason and not _chain_hold("KnowledgeHarvest", hv_reason):
        state.setdefault("harvest_gate_turn", {})[session_id] = _uturn
        _owed = {"owed_count": note_owed(state, session_id)} if v6 else {}
        append_guard_log("knowledge_harvest", {
            "session_id": session_id, "turn_seq": turn_seq,
            "own_mod_files": len(own_mod_files),
            "accessed": len(state.get("accessed_files") or []),
            **_owed,
        })
        write_state(session_id, state)
        output_block(_piggyback(hv_reason))
        return True
    return False


def _harvest_pending_gate(run: _StopRun) -> bool:
    """收割對帳（Harvest-Pending Gate）：收割回報有 item 對不上 receipt → 每回合擋一次；擋了回 True。"""
    state, session_id, turn_seq, v6, last_text, _uturn = run.state, run.session_id, run.turn_seq, run.v6, run.last_text, run.since
    _chain_hold, _piggyback = run._chain_hold, run._piggyback
    # ── Harvest-Pending Gate：收割回報有 item 對不上 receipt ──────
    # post_tool_use 核對落 knowledge_harvest[sid].pending；此處每回合（_user_turn）擋一次要求真做完再重報。
    # 本則是中途狀態句（仍在等 agent 回報…）→ 不擋，等真收尾再擋（wg_harvest.in_progress_text）。
    _hp_waived: List[Dict[str, str]] = []
    hp_reason = pending_gate_reason(state, session_id, turn_seq, last_text, gate_turn=_uturn,
                                    waived=_hp_waived)
    _log_waivers(state, session_id, turn_seq, _uturn, "harvest_pending", _hp_waived)   # 使用者說先這樣／跳過 → 整批放行並留帳
    if hp_reason and _gate_mode("HARVEST_ITEM_NEEDS_RECEIPT", v6) == _REMIND:
        state.setdefault("harvest_pending_gate_turn", {})[session_id] = _uturn
        _remind_logged("harvest_pending", "Harvest-Pending", "HARVEST_ITEM_NEEDS_RECEIPT", hp_reason, session_id, turn_seq,
                       items=(state.get("knowledge_harvest", {}).get(session_id) or {}).get("pending", [])[:5])
        write_state(session_id, state)
    elif hp_reason and not _chain_hold("Harvest-Pending", hp_reason):
        state.setdefault("harvest_pending_gate_turn", {})[session_id] = _uturn
        append_guard_log("harvest_pending", {
            "session_id": session_id, "turn_seq": turn_seq,
            "items": (state.get("knowledge_harvest", {}).get(session_id) or {}).get("pending", [])[:5],
        })
        write_state(session_id, state)
        output_block(_piggyback(hp_reason))
        return True
    return False


def _scan_report_gate(run: _StopRun) -> bool:
    """收尾檢核（Scan-Report Gate）：動 core 檔或多檔且宣告完成，缺 anti_evasion_report → 擋；擋了回 True。"""
    state, config, session_id, turn_seq, v6, stop_count = run.state, run.config, run.session_id, run.turn_seq, run.v6, run.stop_count
    own_mod_files, committed_this_turn, _coding, last_text = run.own_mod_files, run.committed_this_turn, run.coding, run.last_text
    emitted_this_turn, aec_up_to_date, _uturn = run.emitted_this_turn, run.aec_up_to_date, run.since
    _done, _chain_hold, _piggyback = run._done, run._chain_hold, run._piggyback
    # ── Scan-Report Gate ────────────────────────────────────────
    # 降條件觸發 — 只在動 core 檔或多檔（≥min_files_to_block）且宣告完成時要求收尾檢核；
    # 純單檔/文件小改不觸發（避免過度觸發成儀式性負擔，非防退避）。aec.scan_report_gate=false 整道不擋（規則 CLOSING_RITUAL_SWITCHES）。
    if (own_mod_files and not state.get("scan_report_warned") and not committed_this_turn and _scan_report_gate_on(config)
            and (_coding or not v6)):
        recent_prompts = state.get("recent_user_prompts", []) or []
        sr_min_files = int(config.get("min_files_to_block", 2))
        # 門檻達到、本來要擋，使用者近 3 則說了「先這樣／跳過」→ 收尾檢核放行並留帳
        _cc_w = None if (emitted_this_turn or aec_up_to_date or not completion_threshold_met(
            last_text, own_mod_files, sr_min_files, claimed=_done(last_text))) else closing_check_waiver(last_text, recent_prompts)
        _log_waivers(state, session_id, turn_seq, _uturn, "closing_check", [_cc_w] if _cc_w else [])
        _sr_owed = detect_missing_aec_emission(
            last_text, own_mod_files, recent_prompts, sr_min_files, emitted_this_turn or aec_up_to_date, claimed=_done(last_text))
        if _sr_owed and _gate_mode("CLOSING_CHECK_REQUIRED", v6) == _REMIND:
            state["scan_report_warned"] = True          # one-shot 照記（提醒也只一次）
            _remind_logged("scan_report", "ScanReport", "CLOSING_CHECK_REQUIRED",
                           i18n.t("hook.stop.hold_scan_report"), session_id, turn_seq,
                           own_mod_files=len(own_mod_files))
            write_state(session_id, state)
        elif _sr_owed and not _chain_hold("ScanReport", i18n.t("hook.stop.hold_scan_report")):
            state["stop_blocked_count"] = stop_count + 1
            state["scan_report_warned"] = True  # 保留 one-shot anti-nag（比照現況）
            reason = _piggyback(
                i18n.t("hook.stop.scan_report_required") + CLOSING_ONE_CALL

            )
            write_state(session_id, state)
            output_block(reason)
            return True
    return False


def _aec_pending_gate(run: _StopRun) -> bool:
    """報告待寫（AEC-Pending Gate）：收尾檢核把「記憶寫入」推到之後 → 每回合擋一次；擋了回 True。"""
    state, session_id, turn_seq, v6, stop_count, max_blocks = (
        run.state, run.session_id, run.turn_seq, run.v6, run.stop_count, run.max_blocks)
    aec, _coding, emitted_this_turn, _uturn = run.aec, run.coding, run.emitted_this_turn, run.since
    _chain_hold, _piggyback = run._chain_hold, run._piggyback
    # ── AEC-Pending Gate：報告把「記憶寫入」推到之後 ─────────────────
    # (d)「尚未寫／見下一動」、(h)「下一動＝寫 atom」= 把知識留給下一回合（使用者要再問一次
    # 才會補）。post_tool_use 判定落 report["d_pending"]；此處每回合（_user_turn）擋一次（共用 max_blocks
    # 預算）：模型 atom_write 後重新 emit → 新報告無 d_pending → 放行。
    pending = (aec.get("d_pending") or []) if (_coding or not v6) else []
    _ap_owed = bool(emitted_this_turn and pending and state.get("aec_pending_gate_turn") != _uturn)
    if _ap_owed and _gate_mode("CLOSING_CHECK_MEMORY_NOT_DEFERRED", v6) == _REMIND:
        state["aec_pending_gate_turn"] = _uturn
        _remind_logged("aec_pending", "AEC-Pending", "CLOSING_CHECK_MEMORY_NOT_DEFERRED",
                       i18n.t("hook.stop.hold_aec_pending", count=len(pending)), session_id, turn_seq, items=pending[:5])
        write_state(session_id, state)
    elif (
        _ap_owed
        and stop_count < max_blocks
        and not _chain_hold("AEC-Pending", i18n.t("hook.stop.hold_aec_pending", count=len(pending)))
    ):
        state["aec_pending_gate_turn"] = _uturn
        state["stop_blocked_count"] = stop_count + 1
        append_guard_log("aec_pending", {
            "session_id": session_id, "turn_seq": turn_seq, "items": pending[:5],
        })
        write_state(session_id, state)
        output_block(_piggyback(
            i18n.t("hook.stop.aec_pending", count=len(pending), items="\n".join(f"  ✗ {x}" for x in pending[:5]))
        ))
        return True
    return False


def _notice_unseen_report(run: _StopRun) -> None:
    """報告視窗：有退避訊號的收尾報告沒有窗看得到 → 先問插件，沒人接就印一行報告檔在哪（不擋）。"""
    state, config, session_id, turn_seq, aec = run.state, run.config, run.session_id, run.turn_seq, run.aec
    # 有退避訊號的報告沒有窗看得到（沒裝顯示報告的插件，或窗死了）→ 印一行告知報告檔在哪（可觀測性鐵律：不得 fail-silent），
    # 不擋、不把報告內容貼回聊天（設計取捨：報告任何理由都不進聊天）。post_tool_use 標旗，此處消費一次；消費前先問插件一次。
    if state.get("aec_hud_fallback"):
        state["aec_hud_fallback"] = False  # 消費，避免重播
        _file = WORKFLOW_DIR / "aec-report" / f"{session_id}-t{aec.get('turn_seq', turn_seq)}.json"
        if not any(wg_plugins.emit("AecReport.unseen", severity=aec.get("severity", "notable"), report_file=_file,
                                   config=config, session_id=session_id)):
            print(t("hook.stop.aec_report_on_disk", file=_file), file=sys.stderr)
        write_state(session_id, state)


def _reload_advice_gate(run: _StopRun) -> bool:
    """Reload 建議（Reload Advice Gate）：叫使用者 Reload 前查工具服務是不是已載入最新程式；擋了回 True。"""
    state, session_id, turn_seq, v6, stop_count, max_blocks = (
        run.state, run.session_id, run.turn_seq, run.v6, run.stop_count, run.max_blocks)
    transcript, transcript_text, _chain_hold, _piggyback = run.transcript, run.transcript_text, run._chain_hold, run._piggyback
    # ── Reload Advice Gate（規則 RELOAD_ADVICE_ALREADY_LOADED）──────────────
    # 收尾叫使用者 Reload Window 前，查每個活著的工具服務是不是已載入磁碟上最新的程式；都是最新就擋一次。每個使用者回合一次。
    # 只對記憶系統自己的工具服務有意義：本 session 沒改過 tools/workflow-guardian-mcp/ 底下的檔就不判（也不列全機程序表）。
    _rl_turn = _user_turn(state, int(state.get("turn_seq", 0)))
    if (v6 and stop_count < max_blocks and state.get("reload_advice_turn") != _rl_turn
            and _session_touched_mcp(state, session_id)):
        _rl_text = get_last_assistant_text(transcript, text=transcript_text, min_chars=1)
        if RELOAD_ADVICE_RE.search(_rl_text or ""):
            _rl = reload_advice_verdict(_rl_text, *_mcp_load_facts(), time.time())
            if _rl.owed and _gate_mode(_rl.rule, v6) == _REMIND:
                state["reload_advice_turn"] = _rl_turn
                _remind_logged("reload_advice", "ReloadAdvice", _rl.rule, _rl.why, session_id, turn_seq, evidence=list(_rl.evidence)[:4])
                write_state(session_id, state)
            elif _rl.owed and not _chain_hold("ReloadAdvice", _rl.why):
                state["reload_advice_turn"] = _rl_turn
                state["stop_blocked_count"] = stop_count + 1
                write_state(session_id, state)
                output_block(_piggyback(
                    i18n.t("hook.stop.reload_advice", rule=_rl.rule, why=_rl.why, evidence=i18n.t("hook.stop.evidence_sep").join(_rl.evidence))))
                return True
    return False


def _sync_reminder_gate(run: _StopRun) -> bool:
    """同步提醒（Sync Reminder Gate）：本 session 改的檔還沒 commit／push → 擋（回 True）或只留帳。"""
    state, config, session_id, turn_seq, v6, stop_count, phase = (
        run.state, run.config, run.session_id, run.turn_seq, run.v6, run.stop_count, run.phase)
    own_mod_files, aec_covers_sync, _chain_hold, _piggyback = run.own_mod_files, run.aec_covers_sync, run._chain_hold, run._piggyback
    # ── Sync Reminder Gate ──────────────────────────────────────
    sr_config = config.get("sync_reminder", {}) or {}
    sr_enabled = sr_config.get("enabled", True)
    sr_max = int(sr_config.get("max_reminders", 1))
    sr_count = int(state.get("sync_reminder_count", 0))
    # session-filter：只認本 session 自己改的檔（own_mod_files）。共用工作樹下，協調
    # session 不得替他 session 未提交的檔誤觸發同步提醒（比照 Scan-Report 閘的過濾）。
    if (
        sr_enabled
        and own_mod_files
        and not state.get("muted")
        and phase not in ("done", "syncing")
        and sr_count < sr_max
    ):
        uncommitted = _detect_uncommitted_files(own_mod_files)
        unpushed: List[str] = []
        if not uncommitted and sr_config.get("unpushed", True):
            unpushed = _git_unpushed_roots(own_mod_files)
        if (uncommitted or unpushed) and aec_covers_sync:
            # 規則 SYNC_COVERED_BY_CLOSING_CHECK：本回合的收尾檢核 (g) 已交代哪些 commit、哪些沒上與理由（AI 的宣告＋HUD 留帳），
            # 而且交了之後沒再改檔 → 同一件事不再擋一回合；只留 stderr 一行與留帳
            print(i18n.t("hook.stop.sync_covered_by_aec", uncommitted=len(uncommitted), unpushed=len(unpushed)), file=sys.stderr)
            append_guard_log("sync_reminder", {"event": "covered_by_aec", "session_id": session_id, "turn_seq": turn_seq,
                                               "uncommitted": len(uncommitted), "unpushed": len(unpushed)})
        elif (uncommitted or unpushed) and _gate_mode("SYNC_REMINDER_UNSYNCED", v6) == _REMIND:
            state["sync_reminder_count"] = sr_count + 1          # 每 session 提醒次數照 max_reminders 計；不吃共用擋次
            _remind_logged("sync_reminder", "SyncReminder", "SYNC_REMINDER_UNSYNCED",
                           i18n.t("hook.stop.sync_unsynced_summary", uncommitted=len(uncommitted), unpushed=len(unpushed)),
                           session_id, turn_seq, uncommitted=len(uncommitted), unpushed=len(unpushed))
            write_state(session_id, state)
        elif uncommitted and not _chain_hold("SyncReminder", i18n.t("hook.stop.hold_uncommitted", count=len(uncommitted))):
            state["sync_reminder_count"] = sr_count + 1
            state["stop_blocked_count"] = stop_count + 1
            # 訊息瘦身：檔案清單不進 chat（statusline 常駐示數、模型自行 git status）
            reason = _piggyback(
                i18n.t("hook.stop.sync_reminder", count=len(uncommitted))
            )
            write_state(session_id, state)
            output_block(reason)
            return True
        if unpushed and _gate_mode("SYNC_REMINDER_UNSYNCED", v6) == _BLOCK and not _chain_hold(
                "SyncReminder", i18n.t("hook.stop.hold_unpushed", count=len(unpushed))):
            state["sync_reminder_count"] = sr_count + 1
            state["stop_blocked_count"] = stop_count + 1
            reason = _piggyback(
                i18n.t("hook.stop.sync_unpushed", repos=i18n.t("hook.stop.list_sep").join(unpushed))
            )
            write_state(session_id, state)
            output_block(reason)
            return True
    return False


def _atom_audit_gate(run: _StopRun) -> bool:
    """取用稽核（Atom Consumption Audit Gate）：指標卡沒被取用 → 擋（回 True）或只留帳；判定失敗 fail-open。"""
    state, config, session_id, turn_seq, v6, stop_count, max_blocks = (
        run.state, run.config, run.session_id, run.turn_seq, run.v6, run.stop_count, run.max_blocks)
    _chain_hold, _piggyback = run._chain_hold, run._piggyback
    # ── Atom Consumption Audit Gate（取用端閉環稽核）──────────────
    # 排序：correctness / sync 之後（那些優先）、DPM 之前；沿用 stop_gate_max_blocks
    # 全域預算（第 3 次強制放行）。per-atom 一次（atom_audit_prompted）防轟炸。
    # fail-open：判定任何失敗 → stderr 浮訊號後照常放行，不比既有慣例更擋人。
    if (
        (config.get("atom_audit", {}) or {}).get("enabled", True)
        and stop_count < max_blocks
    ):
        try:
            aa = _audit_pointer_atom_consumption(state)
        except Exception as e:
            aa = None
            print(f"[Guardian:AtomAudit] audit error (fail-open): {e}", file=sys.stderr)
        if aa and _gate_mode("ATOM_CONSUMPTION_AUDIT", v6) == _REMIND:
            prompted = state.setdefault("atom_audit_prompted", [])
            prompted.extend(n for n in aa[1] if n not in prompted)
            _remind_logged("atom_audit", "AtomAudit", "ATOM_CONSUMPTION_AUDIT", aa[0], session_id, turn_seq, atoms=list(aa[1])[:5])
            write_state(session_id, state)
        elif aa and not _chain_hold("AtomAudit", aa[0]):
            aa_reason, aa_names = aa
            prompted = state.setdefault("atom_audit_prompted", [])
            prompted.extend(n for n in aa_names if n not in prompted)
            state["stop_blocked_count"] = stop_count + 1
            write_state(session_id, state)
            output_block(_piggyback(aa_reason))
            return True
    return False


def _deep_postmortem_gate(run: _StopRun) -> bool:
    """深度檢討（Deep Post-Mortem Gate）：高 effort 又真失敗、本 session 沒深寫過 → 要求補完整檢討；擋了回 True。"""
    state, config, session_id, turn_seq, v6, stop_count, cwd = (
        run.state, run.config, run.session_id, run.turn_seq, run.v6, run.stop_count, run.cwd)
    last_text, _done, _chain_hold, _piggyback = run.last_text, run._done, run._chain_hold, run._piggyback
    # ── Deep Post-Mortem Gate（Stage 3）────────────────────────
    # (effort 訊號) AND (真失敗訊號) + 本 session 未深寫過 → 注入指令，要 Claude 結束前用
    # atom_write 補完整 post-mortem。deep_postmortem_done 一次性防重複＝獨立預算 1；
    # 排在 correctness/sync gate 之後（那些優先）但★不共用 stop_gate_max_blocks——
    # 否則 Sync+TestFail 先吃光預算就餓死 DPM（見 _should_deep_postmortem docstring 實證）。
    claims_done = _done(last_text)
    _dpm_owed = _should_deep_postmortem(state, config, claims_done) and not _dpm_marker(session_id).exists()
    if _dpm_owed and _gate_mode("DEEP_POSTMORTEM_ON_FAILURE", v6) == _REMIND:
        state["deep_postmortem_done"] = True
        try:
            _dpm_marker(session_id).parent.mkdir(parents=True, exist_ok=True)
            _dpm_marker(session_id).touch()
        except OSError:
            pass
        _remind_logged("deep_postmortem", "DeepPostMortem", "DEEP_POSTMORTEM_ON_FAILURE",
                       i18n.t("hook.stop.hold_postmortem"), session_id, turn_seq)
        write_state(session_id, state)
    elif _dpm_owed and not _chain_hold("DeepPostMortem", i18n.t("hook.stop.hold_postmortem")):
        state["deep_postmortem_done"] = True
        try:
            marker = _dpm_marker(session_id)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()   # 舊 marker 由開 session 時的 _shared.prune_workflow_records 照表清（7 天）
        except OSError:
            pass
        state["stop_blocked_count"] = stop_count + 1
        reason = _piggyback(_dpm_instruction(state, config, cwd))
        write_state(session_id, state)
        output_block(reason)
        return True
    return False


def _attribute_outcome(run: _StopRun) -> None:
    """閉環歸因：前面的正確性閘都沒擋，這回合的對錯訊號已落定 → 把注入的卡歸因成有用或無用，立刻寫 state。"""
    state, config, session_id, transcript, last_text, transcript_text = (
        run.state, run.config, run.session_id, run.transcript, run.last_text, run.transcript_text)
    # ── 注入→使用→結果 閉環歸因（correctness gates 通過後、per-turn 一次性）──
    # 走到這裡代表本 Stop 未被 test-fail/scan-report 等 correctness gate 攔下 →
    # 該 turn 的對錯訊號已落定。turn_seq 守門一次性；write_state 立即固化標記
    # （防後續 phase=done / muted 等不寫 state 的終止路徑導致重複計 α/β）。
    _attribute_usefulness(
        state, config, session_id, transcript, last_text, transcript_text
    )
    write_state(session_id, state)


def _anti_loop_exit(run: _StopRun) -> bool:
    """防連擋（Anti-loop guard）：共用擋次已滿、或 phase 已是 done／syncing → 放行（回 True）。"""
    state, config, session_id, stop_count, max_blocks, phase = (
        run.state, run.config, run.session_id, run.stop_count, run.max_blocks, run.phase)
    # ── Anti-loop guard ─────────────────────────────────────────
    if stop_count >= max_blocks:
        state["phase"] = "done"
        write_state(session_id, state)
        _maybe_spawn_user_extract_worker(session_id, state, config)
        output_nothing()
        return True

    if phase in ("done", "syncing"):
        _maybe_spawn_user_extract_worker(session_id, state, config)
        output_nothing()
        return True
    return False


def _sync_block_gate(run: _StopRun) -> None:
    """底部一般同步擋（最後一步，一定送出口）：改了幾個檔、待記知識、文件漂移 → 擋，或只留 stderr／帳之後放行。"""
    state, config, session_id, turn_seq, v6, stop_count, _since = (
        run.state, run.config, run.session_id, run.turn_seq, run.v6, run.stop_count, run.since)
    own_mod_files, _chain_hold, _piggyback = run.own_mod_files, run._chain_hold, run._piggyback
    # 規則 SYNC_BLOCK_SKIPS_SYNCED：已 commit 並 push 的檔不再進一般 sync block（查不了 git 的照算）
    own_mod_files = run.own_mod_files = _drop_synced(own_mod_files)   # 之後的預覽（_later_gates）也看這份

    # session-filter：底部一般 block 亦只數本 session 自己改的檔（own_mod_files）——
    # 協調 session（own 為空）不因共用樹上他 session 的檔而誤觸發。kq_count 不過濾：
    # knowledge_queue 是本 session 自己的待記知識，即使 own 檔為 0 仍是本 session 責任。
    mod_count = len(own_mod_files)
    kq_count = len(state.get("knowledge_queue", []))
    unique_files = list({m["path"] for m in own_mod_files})
    min_files = config.get("min_files_to_block", 2)

    if state.get("muted"):
        _maybe_spawn_user_extract_worker(session_id, state, config)
        output_nothing()
        return

    if mod_count == 0 and kq_count == 0:
        state["phase"] = "done"
        write_state(session_id, state)
        _maybe_spawn_user_extract_worker(session_id, state, config)
        output_nothing()
        return

    if len(unique_files) < min_files and kq_count == 0:
        state["phase"] = "done"
        write_state(session_id, state)
        _maybe_spawn_user_extract_worker(session_id, state, config)
        output_nothing()
        return

    docdrift = (state.get("docdrift_pending") or {}) if DOCDRIFT_AVAILABLE else {}
    sync_block_owed = _sync_block_items(state, len(unique_files), min_files, _since)
    if not sync_block_owed:
        if kq_count == 0 and not docdrift:
            # 只剩「改了 N 個檔」：跟同步提醒、收尾檢核 (g) 是同一件事（規則 SYNC_BLOCK_NOT_REPEATED），只留 stderr 一行
            print(i18n.t("hook.stop.sync_block_not_repeated", count=len(unique_files)), file=sys.stderr)
        else:
            # 規則 SYNC_BLOCK_NEW_ITEMS_ONLY：這批待記知識／文件漂移前面的使用者回合擋過了，沒有新的一筆
            print(i18n.t("hook.stop.sync_block_new_items_only", knowledge=kq_count, drift=len(docdrift)), file=sys.stderr)
        state["phase"] = "done"
        write_state(session_id, state)
        _maybe_spawn_user_extract_worker(session_id, state, config)
        output_nothing()
        return

    if _gate_mode("SYNC_BLOCK_PENDING_ITEMS", v6) == _REMIND:
        # 提醒：這批待記知識／文件漂移記成提醒過（SYNC_BLOCK_NEW_ITEMS_ONLY 下次只看新的），只留帳
        _prev, _mark = state.get("sync_block_shown") or {}, _turn_mark(state, _since)
        state["sync_block_shown"] = {k: _prev.get(k, _mark) for k in _sync_block_keys(state, len(unique_files), min_files)}
        _remind_logged("sync_block", "SyncBlock", "SYNC_BLOCK_PENDING_ITEMS",
                       i18n.t("hook.stop.hold_sync_block3", files=len(unique_files), knowledge=kq_count, drift=len(docdrift)), session_id, turn_seq)
        state["phase"] = "done"
        write_state(session_id, state)
        _maybe_spawn_user_extract_worker(session_id, state, config)
        output_nothing()
        return

    if _chain_hold("SyncBlock", i18n.t("hook.stop.hold_sync_block2", files=len(unique_files), knowledge=kq_count)):
        write_state(session_id, state)
        _maybe_spawn_user_extract_worker(session_id, state, config)
        output_nothing()
        return

    state["stop_blocked_count"] = stop_count + 1
    # 只留這次還欠的項目（已解決、已清掉的不留著越記越多）；前面回合擋過的保留第一次擋的回合
    _prev, _mark = state.get("sync_block_shown") or {}, _turn_mark(state, _since)
    state["sync_block_shown"] = {k: _prev.get(k, _mark) for k in _sync_block_keys(state, len(unique_files), min_files)}

    file_names = ", ".join(f.rsplit("/", 1)[-1] for f in unique_files[:8])

    reason = (
        f"[Workflow Guardian] {len(unique_files)} file(s) modified"
        + (f", {kq_count} knowledge pending" if kq_count else "")
        + f". Files: {file_names}.\n"
        "Sync: _AIDocs→_CHANGELOG | knowledge→atom | .git→add+commit+push | .svn→add+commit"
    )

    if DOCDRIFT_AVAILABLE:
        try:
            dp = state.get("docdrift_pending", {})
            if dp:
                docs = sorted(set(v["doc"] for v in dp.values()))
                reason += f"\n[DocDrift] {len(dp)} source change(s) → consider updating: {', '.join(docs[:5])}"
        except Exception:
            pass

    reason = _piggyback(reason)
    write_state(session_id, state)
    _maybe_spawn_user_extract_worker(session_id, state, config)

    output_block(reason)
