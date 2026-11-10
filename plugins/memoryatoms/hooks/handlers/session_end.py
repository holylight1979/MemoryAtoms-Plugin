"""
handlers/session_end.py — SessionEnd hook handler

收尾：DocDrift summary / state cleanup / extract worker / user-extract worker /
session evaluator / iteration metrics / oscillation / self-iterate atoms /
wisdom reflect / staging reminder / conflict detection / atom health fix /
review marker / vector reindex / vcs-sync worker spawn（記憶庫上版控）。
"""

import json
import sys
from typing import Any, Dict

from wg_core import (
    CLAUDE_DIR, CODE_DIR, WORKFLOW_DIR,
    _ensure_state, _now_iso, write_state,
    _atom_debug_error, v6_enabled, guard_family_on,
    get_project_memory_dir, resolve_staging_dir,
)
import i18n  # lib/（wg_core 已放進 sys.path）：給人看的提示走語言表
from wg_atoms import (
    _self_iterate_atoms, _trigger_incremental_index, _sweep_realm_auto_migrate,
)
from wg_extraction import _spawn_extract_worker
from wg_session_checks import _detect_atom_conflicts
import wg_plugins
from wg_lazy_re import lazy_module
chain_store = lazy_module("chain_store")   # 連鎖斷路器（守門家族，插件版不出廠）：守門家族開才清它的快照與帳
from wg_recall_miss import detect_recall_misses
from wg_evasion import (
    evaluate_session, flush_outcome_stats,
    _collect_iteration_metrics, _detect_oscillation, _save_oscillation_state,
    _save_review_marker, _tally_session_for_review,
)
from handlers._shared import (
    _cleanup_old_states,
    _maybe_spawn_user_extract_worker,
    DOCDRIFT_AVAILABLE, build_drift_advisory,
)


def _se_sentinel_path(session_id: str):
    # 取 wg_core.WORKFLOW_DIR 即時值（非 import 時綁定）：測試 monkeypatch 後仍生效
    import wg_core
    return wg_core.WORKFLOW_DIR / "se-sentinel" / f"{session_id}.json"


def _se_sentinel_arm(session_id: str) -> None:
    """SessionEnd 開頭 touch 哨兵、結尾清（_se_sentinel_clear）。哨兵殘留＝
    收尾流程未跑完即中斷（harness timeout / 例外）→ 下個 SessionStart 檢殘留
    浮一行告警（比照 UPS 哨兵；可觀測性鐵律：擁擠超時不得靜默）。fail-open。"""
    if not session_id:
        return
    try:
        p = _se_sentinel_path(session_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"at": _now_iso()}, ensure_ascii=False), encoding="utf-8", newline="\n")
    except OSError:
        pass


def _se_sentinel_clear(session_id: str) -> None:
    if not session_id:
        return
    try:
        p = _se_sentinel_path(session_id)
        if p.exists():
            p.unlink()
    except OSError:
        pass


WORKER_ENV = "WG_SESSION_END_WORKER"   # 背景 worker 自己跑本 handler 時帶著：不再派第二層


def _detach_to_worker(input_data: Dict[str, Any]) -> bool:
    """插件版：SessionEnd 預算硬上限 1.5 秒（官方 hooks.md：插件 hook 的 timeout 拉不高），所以只派一支脫離的背景
    worker（hooks/session_end_worker.py）跑下面整段收尾，自己立刻返回。安裝版、worker 自己、派不起來時回 False＝照舊在這裡跑。"""
    import os
    from edition import edition, PLUGIN
    if edition() != PLUGIN or os.environ.get(WORKER_ENV):
        return False
    import subprocess
    import wg_core
    from wg_platform import IS_WINDOWS, no_window_flags, DETACHED_PROCESS
    worker = wg_core.CODE_DIR / "hooks" / "session_end_worker.py"
    try:
        logs = wg_core.CLAUDE_DIR / "Logs"
        logs.mkdir(parents=True, exist_ok=True)
        inbox = wg_core.WORKFLOW_DIR / "se-worker"
        inbox.mkdir(parents=True, exist_ok=True)
        job = inbox / f"{input_data.get('session_id', '') or 'nosession'}-{os.getpid()}.json"
        job.write_text(json.dumps(input_data, ensure_ascii=False), encoding="utf-8", newline="\n")
        env = os.environ.copy()
        env[WORKER_ENV] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        if IS_WINDOWS:
            kw = {"creationflags": no_window_flags() | DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
        else:
            kw = {"start_new_session": True}
        with open(logs / "session-end-worker.log", "a", encoding="utf-8", newline="\n") as fh:
            subprocess.Popen([sys.executable, str(worker), str(job)], stdin=subprocess.DEVNULL,
                             stdout=fh, stderr=fh, env=env, cwd=str(wg_core.CODE_DIR), close_fds=True, **kw)
        return True
    except Exception as e:   # 派不起來：浮 stderr，退回就地跑（可能被 1.5 秒砍，但不無聲）
        print(i18n.t("hook.session_end.worker_spawn_failed", error=e), file=sys.stderr)
        _atom_debug_error("session_end:spawn_worker", e)
        return False


def handle_session_end(input_data: Dict[str, Any], config: Dict[str, Any]) -> None:
    if _detach_to_worker(input_data):
        return
    session_id = input_data.get("session_id", "")
    state = _ensure_state(session_id, input_data, config)
    if not state:
        sys.exit(0)
        return

    _se_sentinel_arm(session_id)
    v6 = v6_enabled(config)     # 關＝只跑 9e476fd0 就有的收尾
    guard = guard_family_on(config)   # 關（插件版）＝DocDrift 摘要、連鎖斷路器清理、驗證收據清理不跑；萃取、衝突偵測、向量重建照跑

    state["ended_at"] = _now_iso()
    state["phase"] = "done"

    if guard and DOCDRIFT_AVAILABLE and config.get("docdrift", {}).get("enabled", True):
        try:
            drift_msg = build_drift_advisory(state, config)
            if drift_msg:
                print(f"[Guardian:DocDrift] {drift_msg}", file=sys.stderr)
        except Exception as e:
            _atom_debug_error("session_end:docdrift_advisory", e)

    ue_config = config.get("userExtraction", {})
    if not ue_config.get("enabled", False) and state.get("pending_user_extract"):
        state["pending_user_extract"] = []

    try:
        _cleanup_old_states()
    except Exception as e:
        print(f"SessionEnd cleanup error: {e}", file=sys.stderr)
    if guard:
        chain_store.session_cleanup(session_id)   # 連鎖斷路器：這個 session 的快照；7 天沒動的別 session 快照與連鎖帳（刪不掉浮 stderr）
    try:
        # run_verify 自寫的驗證收據（workflow/verify-ledger/_runs/）：留最新 200 份、刪 30 天以上；單份刪不掉在函式內浮 stderr
        if v6 and guard:
            from wg_core import CLAUDE_DIR as _CD
            from self_receipt import prune_self_runs
            prune_self_runs(_CD)
    except Exception as e:
        print(i18n.t("hook.session_end.sessionend_self_written_receipt", etype=type(e).__name__, error=e), file=sys.stderr)

    rc = config.get("response_capture", {})
    # session_end 全文萃取 worker 的唯一下游是 session_end_flush（已停產）；
    # gate 綁 flush.enabled，避免每次 SessionEnd 白燒本機 LLM 30-60s 產出被 DEVNULL 丟棄的結果。
    # failure 萃取走獨立路徑，不受此 gate 影響。
    _sef_enabled = rc.get("session_end_flush", {}).get("enabled", True)
    if rc.get("enabled", True) and _sef_enabled:
        cwd = state.get("session", {}).get("cwd", "")
        tracker = state.get("topic_tracker", {})
        dist = tracker.get("intent_distribution", {})
        intent = max(dist, key=dist.get, default="build") if dist else "build"
        worker_ctx = {
            "session_id": session_id,
            "cwd": cwd,
            "config": config,
            "knowledge_queue": state.get("knowledge_queue", []),
            "session_intent": intent,
            "byte_offset": state.get("extraction_offset", 0),
        }
        pid = _spawn_extract_worker(worker_ctx)
        if pid:
            print(f"extract-worker spawned (pid={pid}, intent={intent})", file=sys.stderr)
        state["extract_worker_pid"] = 0

    # 效用歸因遙測：outcome unknown 比率落 workflow/outcome_stats.jsonl；連續偏高
    # → 寫 advisory marker（SessionStart 讀後注入並清除，同 realm automove 模式）。
    # 防換模型後完成語 regex 失配 → unknown 全吞 → α/β 晉升軌靜默停滯。
    try:
        _ow_advisory = flush_outcome_stats(state, config, session_id)
        if _ow_advisory:
            _ow_marker = WORKFLOW_DIR / "outcome-unknown-advisory.json"
            _ow_marker.write_text(
                json.dumps({"msg": _ow_advisory, "at": _now_iso()}, ensure_ascii=False),
                encoding="utf-8",
            newline="\n")
            print(_ow_advisory, file=sys.stderr)
    except Exception as e:
        _atom_debug_error("session_end:outcome_watch", e)

    worker_spawned = _maybe_spawn_user_extract_worker(session_id, state, config)

    if not worker_spawned and ue_config.get("enabled", False):
        try:
            evaluate_session(session_id, state, config, worker_stats=None)
        except Exception as e:
            _atom_debug_error("session_evaluator_fallback", e)

    mod_count = len(state.get("modified_files", []))
    kq_count = len(state.get("knowledge_queue", []))
    if state.get("sync_pending") and (mod_count > 0 or kq_count > 0):
        print(
            f"Warning: Session ending with unsaved work. "
            f"{mod_count} modified files, {kq_count} knowledge items.",
            file=sys.stderr,
        )

    try:
        metrics = _collect_iteration_metrics(state)
        state["iteration_metrics"] = metrics

        oscillations = _detect_oscillation(state, config)
        if oscillations:
            state["iteration_metrics"]["oscillations"] = oscillations
            for osc in oscillations:
                print(
                    f"Oscillation detected: {osc['atom']} "
                    f"({osc['count']} sessions)",
                    file=sys.stderr,
                )
        _save_oscillation_state(oscillations if oscillations else [], state, config)
        _tally_session_for_review(state)
    except Exception as e:
        print(f"Self-iteration metrics error: {e}", file=sys.stderr)

    vcs_sync_reason = "session_end"
    try:
        si_results = _self_iterate_atoms(state, config)
        if si_results.get("promoted"):
            for p in si_results["promoted"]:
                print(
                    i18n.t("hook.session_end.auto_promoted", t_from="[臨]", t_to="[觀]", atom=p['atom'], count=len(p['items'])),   # [臨]/[觀] 是卡片信心標記，不翻
                    file=sys.stderr,
                )
            vcs_sync_reason = "promotion"
        if si_results.get("archive_candidates"):
            _fr = si_results.get("forget") or {}
            if _fr.get("mode") == "isolated" and _fr.get("forgotten"):
                print(
                    i18n.t("hook.session_end.selective_forget_isolated_stale", count=len(_fr['forgotten'])),
                    file=sys.stderr,
                )
            else:
                # 報告落候選 atom 所屬記憶庫的 _staging（全域候選 → ~/.claude/memory/_staging，
                # 非 cwd 專案庫），故印實際路徑而非相對 "_staging/"。
                for rp in si_results.get("reports") or []:
                    print(
                        i18n.t("hook.session_end.archive_candidates_atoms_low", count=len(si_results['archive_candidates']), rp=rp),
                        file=sys.stderr,
                    )
            # 隔離後的索引／catalog 收尾若失敗，檔已搬走但索引或 catalog 還指著舊位置——
            # 不能無聲吞掉（可觀測性鐵律），逐條浮到 stderr 讓下個 session 看得到。
            for ie in _fr.get("index_errors") or []:
                print(f"Selective-forget index cleanup FAILED: {ie}", file=sys.stderr)
            for ce in _fr.get("catalog_errors") or []:
                print(f"Selective-forget catalog regen FAILED: {ce}", file=sys.stderr)
    except Exception as e:
        print(f"Self-iteration error: {e}", file=sys.stderr)

    # V5+ Realm 維度：自動歸類 sweep（高信心 core→local；永不靜默，下個 SessionStart 提示）
    try:
        realm_moved = _sweep_realm_auto_migrate(config)
        if realm_moved:
            for m in realm_moved:
                print(
                    f"[realm] auto-migrated {m['slug']} → local/{m['domain']} "
                    f"({m['from']} → {m['to']})",
                    file=sys.stderr,
                )
    except Exception as e:
        print(f"[realm] auto-migrate sweep error: {e}", file=sys.stderr)

    try:
        edit_counts = state.get("edit_counts", {})
        if edit_counts:
            reverted = sum(1 for c in edit_counts.values() if c >= 2)
            if reverted > 0:
                print(
                    f"Over-engineering: {reverted}/{len(edit_counts)} files "
                    f"edited 2+ times",
                    file=sys.stderr,
                )
    except Exception as e:
        print(f"Over-engineering metrics error: {e}", file=sys.stderr)

    cwd = state.get("session", {}).get("cwd", "")
    staging_dir = resolve_staging_dir(cwd)

    wg_plugins.emit("SessionEnd", state=state, config=config, session_id=session_id, cwd=cwd, staging_dir=staging_dir)

    if staging_dir.exists():
        staging_files = list(staging_dir.glob("*.md"))
        if staging_files:
            print(
                i18n.t("hook.session_end.staging_has_temporary_files", count=len(staging_files)),
                file=sys.stderr,
            )

    try:
        conflict_warnings = _detect_atom_conflicts(state, config)
        if conflict_warnings:
            state["conflict_warnings"] = conflict_warnings
            for cw in conflict_warnings:
                print(
                    f"Conflict: {cw['source']} ↔ {cw['target']} "
                    f"(score={cw['score']})",
                    file=sys.stderr,
                )
    except Exception as e:
        print(f"Conflict detection error: {e}", file=sys.stderr)


    # ── 失念偵測（recall-miss）：本 session 有失敗證據、庫中未注入 atom 的 trigger
    # 卻命中 ≥2 詞 → 落 Logs/recall-miss.jsonl（浮出走 effect-report D 節 + 週健檢黃燈）。
    # 純本地字串比對、fail-open 浮訊號，不阻斷收尾。
    if config.get("recall_miss", {}).get("enabled", True):
        try:
            _rm = detect_recall_misses(session_id, state)
            if _rm:
                _rm_names = ", ".join(r["atom"] for r in _rm[:5])
                print(
                    i18n.t("hook.session_end.recall_miss_atoms_should", count=len(_rm), _rm_names=_rm_names),
                    file=sys.stderr,
                )
        except Exception as e:
            print(f"[recall-miss] error: {e}", file=sys.stderr)
            _atom_debug_error("session_end:recall_miss", e)

    # 規則 READ_FAST_PATH：session 最後幾次 Read 還躺在 read-log（之後沒有非 Read 事件來補帳）→ 這裡補完、檔也清掉
    # （讀檔帳與工具結果體積才算完整；有累積才載 post_tool_use）
    try:
        import wg_read_fast
        if wg_read_fast.pending(session_id):
            from handlers.post_tool_use import replay_read_log
            replay_read_log(state, session_id, config, v6)
    except Exception as e:
        _atom_debug_error("session_end:read_fast_replay", e)

    # ── 工具結果體積：整場 per-tool 次數／總量／最大值聚合成一筆
    # Logs/guard-tool-result-stats.jsonl，per-session 暫存檔聚合後刪。純本地、fail-open。
    try:
        from wg_friction import flush_tool_result_stats
        _trs = flush_tool_result_stats(session_id, state)
        if _trs and _trs.get("oversized_n"):
            print(
                i18n.t("hook.session_end.tool_result_calls_totaling", _trs=_trs['calls'], _trs2=_trs['total_chars'] // 1000, _trs3=_trs['oversized_n'], _trs4=_trs['oversized_chars'] // 1000),
                file=sys.stderr,
            )
    except Exception as e:
        _atom_debug_error("session_end:tool_result_stats", e)

    if state.get("review_due"):
        try:
            print(f"Review marker saved (total={_save_review_marker()})", file=sys.stderr)
        except Exception as e:
            print(f"Review marker save error: {e}", file=sys.stderr)

    write_state(session_id, state)

    modified = state.get("modified_files", [])
    has_atom_changes = any(
        "/memory/" in m.get("path", "").replace("\\", "/")
        and m.get("path", "").endswith(".md")
        for m in modified
    )
    if has_atom_changes:
        _trigger_incremental_index(config)

    # 記憶庫上版控：晉升 sweep／realm 搬移全在上面跑完，這裡只 spawn 一次
    # （同 root 的 worker 以鎖＋dirty 合併請求，分兩次 spawn 只會多一個立刻退出的行程）。
    # reason 標 promotion 時代表本次含晉升改動；worker 不區分流程，只影響 log 與起訖帳。
    try:
        from wg_vcs_sync import spawn_vcs_sync
        _vs_pid = spawn_vcs_sync(session_id, cwd, reason=vcs_sync_reason)
        if _vs_pid:
            print(f"vcs-sync worker spawned (pid={_vs_pid}, reason={vcs_sync_reason})", file=sys.stderr)
    except Exception as e:
        # spawn 失敗絕不能拖垮 SessionEnd；改動留在工作樹，下個 SessionStart advisory 會浮出
        print(i18n.t("hook.session_end.vcs_sync_worker_failed", error=e), file=sys.stderr)

    # 走到這裡＝收尾完整跑完 → 拆哨兵（timeout 砍掉時到不了這行，哨兵殘留告警）
    _se_sentinel_clear(session_id)

    sys.exit(0)
