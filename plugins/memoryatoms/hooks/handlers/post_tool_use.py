"""
handlers/post_tool_use.py — PostToolUse hook handler

追蹤 modified_files / vcs_queries（accessed_files 由 Stop 端從 transcript 尾段
一次回收，matcher 不含 Read——省去每次讀檔一個 hook 行程）；
偵測測試失敗、_CHANGELOG 自動 roll、staging 命名、路徑強制、docdrift、hot cache mid-turn 注入。
handle_post_tool_use 是目錄（一行一步）；它的各步照執行順序排在檔尾，步驟之間共用的材料放在 _PostRun。
"""

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from wg_core import (
    _ensure_state, _now_iso, write_state, output_json, output_nothing,
    _atom_debug_error, WORKFLOW_DIR, v6_enabled, guard_family_on,
)
import i18n   # lib/：給人看的提示走語言表
from wg_platform import background_spawn_kwargs  # noqa: E402
from wg_session_checks import _check_output_quality
from wg_extraction import _is_lease_valid  # noqa: F401
from wg_hook_timing import lap as _hook_lap
from wg_evasion import is_test_command, aec_severity, crosscheck_aec_severity, _aec_blank, aec_pending_items
from wg_extraction import is_plan_filename
from wg_harvest import parse_receipt, record_atom_op, validated_this_turn, is_error_response
from wg_lazy_re import lazy_func, lazy_module
# 寫碼守門的模組第一次用到才載（HOOK_IMPORT_ON_USE）：守門關、或這次工具結果走不到那段記帳，就一行都不載
chain_store = lazy_module("chain_store")    # 連鎖斷路器的帳（lib/；判定在 hooks/wg_chain.py）：工具後記 AI 動作
_vl = lazy_module("wg_verify_ledger")       # 驗證收據、哨兵、改檔內容雜湊
_rr = lazy_module("wg_release_rules")       # 測試子命令的切段與認法，紅測範圍跟涵蓋判法用同一份
try:
    # Bash／PowerShell 改檔目標擷取（wg_core）；載不進來就只記 Edit／Write／MultiEdit／NotebookEdit 目標
    from wg_core import write_paths_from_tool as _write_paths_from_tool
except ImportError:
    _write_paths_from_tool = None

# 惰性載入（HOOK_IMPORT_ON_USE）：只有改了 atom、收割 validated 才用到的兩支，每次 PostToolUse 不先載（wg_atoms 約 20 ms、
# wg_vcs_sync 約 18 ms）；名字留在模組上給測試換。
def _trigger_incremental_index(*a: Any, **k: Any) -> Any:
    from wg_atoms import _trigger_incremental_index as _f
    return _f(*a, **k)

aec_ledger = lazy_module("handlers.aec_ledger")   # 殘檔帳本／產出物帳本（守門家族）：用到才載
import wg_read_fast  # 規則 READ_FAST_PATH：Read 事件只落 read-log，下一個事件在這裡補帳
import wg_plugins
from handlers._shared import (
    _is_ephemeral_path, note_ephemeral_skip,
    track_retry,
    DOCDRIFT_AVAILABLE, check_source_drift, resolve_doc_update, prune_committed_entries,
)
# 拆出去的各件事（各檔檔頭有白話）；入口只留流程。下面幾個名字也是別處（pre_tool_use、session_end、subagent_start）從這裡取的
from handlers.post_tool_use_subagent import _record_subagent_injection, _SUBAGENT_INJ_CAP  # noqa: F401
from handlers.post_tool_use_harvest import _apply_harvest, _write_aec_report_file   # 收割回報核對（記憶本體）
# 守門家族（收尾檢核、搬目錄、紅測與哨兵；插件版不出廠）：名字留在模組上給測試換，呼叫時才載
_collect_aec_evidence = lazy_func("handlers.post_tool_use_aec", "_collect_aec_evidence")
_note_moved_code_dirs = lazy_func("handlers.post_tool_use_move", "_note_moved_code_dirs")
_note_move_scan_hits = lazy_func("handlers.post_tool_use_move", "_note_move_scan_hits")
_track_test_result = lazy_func("handlers.post_tool_use_verify", "_track_test_result")
_track_sentinels = lazy_func("handlers.post_tool_use_verify", "_track_sentinels")
from handlers.post_tool_use_writes import (
    _VCS_COMMIT_RE, EXEC_TURN_KEYS_CAP, _note_commit, _note_modified, _note_launcher, _note_unknown_write, _note_failed_writes,
)
from handlers.post_tool_use_reads import (  # noqa: F401
    _note_tool_result, _read_tracking, replay_read_log, replay_reads_before_tool,
)


def _maybe_sync_skill_index(file_path: str, config: Dict[str, Any]) -> None:
    """Detached `skill-index.py --write` when a skills/*/SKILL.md is added/edited.

    skill 計數 SoT 自動同步：重生 _skill_index.json + 重寫文件 marker。Bash 刪除等
    本 hook 漏接的情況由 SessionStart --check 防呆。Fail-open。"""
    try:
        normalized = file_path.replace("\\", "/")
        if "/skills/" not in normalized or not normalized.endswith("/SKILL.md"):
            return
        cfg = (config or {}).get("skill_index", {}) or {}
        if not cfg.get("enabled", True):
            return
        tool_path = Path(__file__).resolve().parent.parent.parent / "tools" / "skill-index.py"
        if not tool_path.exists():
            return
        bg_kwargs: dict = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        bg_kwargs.update(background_spawn_kwargs())
        subprocess.Popen([sys.executable, str(tool_path), "--write"], **bg_kwargs)
    except Exception as e:
        _atom_debug_error("post_tool_use:skill_index_sync", e)
        pass


def _run_companion_hooks(input_data: Dict[str, Any], config: Dict[str, Any]) -> List[str]:
    """原 standalone PostToolUse hook（version_guard / acceptance_spec）併入本程序，省每次
    Edit/Write 多起兩支 Python。各自 try/except 隔離，錯誤只進 debug log、不影響 guardian 主流程。
    回 version_guard 的 systemMessage 訊息（acceptance_spec 只登記規格檔路徑、不送字）；兩檔仍保留 __main__ 可獨跑——
    回滾＝settings.json 的 PostToolUse 加回那兩行。"""
    sys_msgs: List[str] = []
    if not guard_family_on(config):   # 插件版不帶這兩支
        return sys_msgs
    try:
        import version_guard
        sys_msgs = list(version_guard.run(input_data, config.get("version_guard", {})))
    except Exception as e:
        _atom_debug_error("post_tool_use:version_guard", e)
    try:
        import acceptance_spec
        acceptance_spec.run(input_data, config.get("acceptance_spec", {}))
    except Exception as e:
        _atom_debug_error("post_tool_use:acceptance_spec", e)
    return sys_msgs


def _emit_post_tool_output(advisories: List[str], sys_msgs: List[str]) -> None:
    """單一出口：additionalContext（給模型）＋ systemMessage／stderr（給使用者，version_guard 原格式）。"""
    for m in sys_msgs:
        print(m, file=sys.stderr)
    out: Dict[str, Any] = {}
    if sys_msgs:
        out["systemMessage"] = "\n".join(sys_msgs)
    if advisories:
        out["hookSpecificOutput"] = {
            "hookEventName": "PostToolUse",
            "additionalContext": "\n".join(advisories),
        }
    if out:
        output_json(out)
    else:
        output_nothing()


def _chain_action(state: Dict[str, Any], input_data: Dict[str, Any], config: Dict[str, Any], tool_response: Any) -> None:
    """連鎖斷路器：這次工具呼叫算 AI 動作（判法 wg_chain.action_of）就記進連鎖帳，連鎖段開著才記（chain_store.action_done）。
    這個 session 還沒有連鎖帳就跳過（不可能有開著的連鎖段）。只記錄，不影響這次工具；出錯 chain_store 自己浮 stderr。"""
    sid = str(input_data.get("session_id") or "")
    if not sid or not chain_store.ledger_path(sid).exists():
        return
    import wg_chain
    from wg_release_rules import user_turn
    tool = str(input_data.get("tool_name") or "")
    ti = input_data.get("tool_input") or {}
    writes: List[str] = []
    unknown = ran = False
    try:
        if tool in ("Bash", "PowerShell"):
            cwd = input_data.get("cwd", "") or state.get("session", {}).get("cwd", "") or ""
            for p in (_write_paths_from_tool(tool, ti, cwd) if _write_paths_from_tool else []) or []:
                if p.startswith("UNKNOWN:"):
                    unknown = True
                elif p:
                    writes.append(p)
            ran = is_test_command(str(ti.get("command") or "")) or bool(_vl.parse_sentinels(_vl.response_text(tool_response)))
        act = wg_chain.action_of(tool, ti, tool_response, writes, unknown, ran)
    except Exception as e:  # noqa: BLE001  判不出這次算不算動作：這筆不記，工具記帳照舊
        print(i18n.t("hook.post_tool_use.chain_breaker_failed", error=f"{type(e).__name__}: {e}"), file=sys.stderr)
        return
    if act is not None:
        kind, files, what = act
        chain_store.action_done(sid, user_turn(state, int(state.get("turn_seq", 0) or 0)), kind, tool, files, input_data,
                                config, what=what)


def handle_judge_post_tool_use(input_data: Dict[str, Any], config: Dict[str, Any]) -> None:
    """裁判子 session（CLAUDE_COMPANION_JUDGE=1）的 PostToolUse 最小記帳：只記改了哪些檔、目標不明的寫入、commit 回合與驗證收據，
    給 stop.handle_judge_stop 的寫碼查核閘用；不注入、不跑其他閘、不建完整 state（不重建 atom index，防裁判觸發裁判）。"""
    session_id = input_data.get("session_id", "") or ""
    if not session_id:
        output_nothing()
        return
    from wg_core import read_state as _read_state
    tool_name = input_data.get("tool_name", "") or ""
    tool_input = input_data.get("tool_input") or {}
    cwd = input_data.get("cwd", "") or ""
    state = _read_state(session_id) or {"session": {"id": session_id, "cwd": cwd}, "turn_seq": 0, "judge": True}
    if tool_name in ("Write", "Edit", "NotebookEdit", "MultiEdit"):
        fp = tool_input.get("file_path", "") or tool_input.get("notebook_path", "")
        if fp and not _is_ephemeral_path(fp):
            _note_modified(state, session_id, fp, tool_name)
    elif tool_name in ("Bash", "PowerShell"):
        command = tool_input.get("command", "") or ""
        targets = _write_paths_from_tool(tool_name, tool_input, cwd) if _write_paths_from_tool is not None else []
        for p in targets or []:
            if p and p.startswith("UNKNOWN:"):
                _note_unknown_write(state, session_id, p, command, tool_name, "", input_data)
            elif p and not _is_ephemeral_path(p):
                _note_modified(state, session_id, p, tool_name)
        _note_commit(state, command, config, session_id)
        _track_sentinels(state, input_data, command, tool_name, config, session_id)
    write_state(session_id, state)
    output_nothing()


def handle_post_tool_use_failure(input_data: Dict[str, Any], config: Dict[str, Any]) -> None:
    """PostToolUseFailure：工具失敗時 Claude Code 不送 PostToolUse、改送這個。Bash／PowerShell 指令非零結束時：
    - 前段已經寫下去的檔照樣進 modified_files（寫碼查核讀它）；例：`cat > a.py <<EOF … && pytest`
      測試紅了，a.py 已經改了。目標用成功路徑同一支 write_paths_from_tool 算；失敗的指令不一定寫到，只記這次開始後改過的現存檔，目標不明的不記。
    - 測試指令紅了記紅測（failing_tests）與驗證收據，跟成功路徑同一套函式；失敗事件沒有 tool_response，錯誤全文（"Exit code N\\n輸出…"）
      在 error 欄，當字串回應餵進去。error 裡讀不到結束碼（指令沒跑或沒跑完，例：被 hook 擋下）不算測試結果。
    只記帳不擋、不送提示；裁判子 session 只讀既有 state（不重建 atom index）、不記紅測。"""
    tool_name = input_data.get("tool_name", "") or ""
    session_id = input_data.get("session_id", "") or ""
    if tool_name not in ("Bash", "PowerShell") or not session_id or not guard_family_on(config):   # 守門家族關：沒有寫碼查核要記
        output_nothing()
        return
    cwd = input_data.get("cwd", "") or ""
    judge = bool(os.environ.get("CLAUDE_COMPANION_JUDGE"))
    if judge:
        from wg_core import read_state as _read_state
        state = _read_state(session_id) or {"session": {"id": session_id, "cwd": cwd}, "turn_seq": 0, "judge": True}
    else:
        state = _ensure_state(session_id, input_data, config)
    if not state:
        output_nothing()
        return
    aid = str(input_data.get("agent_id") or "")
    command = str((input_data.get("tool_input") or {}).get("command") or "")
    dirty = (_note_failed_writes(state, input_data, tool_name, session_id, aid, cwd)[1] if _write_paths_from_tool is not None
             else False)
    event = {**input_data, "tool_response": str(input_data.get("error") or "")}
    if _vl.exit_code_of(event["tool_response"]) is not None:
        if not judge and _track_test_result(state, event, command, tool_name, config):
            dirty = True
        try:
            if _track_sentinels(state, event, command, tool_name, config, session_id):
                dirty = True
        except Exception as e:  # noqa: BLE001
            print(f"[verify-ledger] error (fail-open): {e}", file=sys.stderr)
            _atom_debug_error("post_tool_use_failure:verify_ledger", e)
    if dirty:
        write_state(session_id, state)
    if not judge and v6_enabled(config) and _vl.exit_code_of(event["tool_response"]) is not None:
        _chain_action(state, input_data, config, event["tool_response"])     # 指令真的跑了（紅的重跑也是動作）
    output_nothing()


def handle_post_tool_use(input_data: Dict[str, Any], config: Dict[str, Any]) -> None:
    """PostToolUse 主流程的目錄，一行一步；各步的函式照執行順序排在下面，步驟之間共用的材料放在 _PostRun（欄位旁註明由哪一步填）。
    分段計時照 _hook_lap 的打點：讀 state → 工具記帳 → 讀檔帳（在 _read_tracking 裡）→ 綜觀與召回 → 寫 state → 插件提示 → 同伴檢查。"""
    session_id = input_data.get("session_id", "")
    state = _ensure_state(session_id, input_data, config)
    if not state:
        _emit_post_tool_output([], _run_companion_hooks(input_data, config))
        return
    _hook_lap("讀 state")
    run = _open_run(input_data, config, session_id, state)

    replay_ctx = _note_every_result(run)    # 每則工具結果都記：Read 補帳、救援日誌、工具結果體積
    _note_subagent(run)                     # Agent／Task：子代理注入歸因
    _edited_file_hooks(run)                 # Edit／Write：改檔插件點、skill 索引
    _note_ledgers(run)                      # 殘檔帳本（寫碼守門關）／產出物帳本（寫碼守門開）
    _record_by_tool(run)                    # 依工具種類記帳：改檔／指令／收尾檢核／卡片操作收據／收割回報

    # 分段計時（wg_hook_timing）：累計過 hook 上限的 80% → degraded，後面只記不問——提示類段落（depends 召回、
    # 跨 session 碰撞、同伴檢查）不跑；docdrift 清帳、寫 state 照做
    degraded = _hook_lap("工具記帳")
    _prune_docdrift(run)

    read_ctx, degraded = _overview_and_recall(run, degraded)
    _hook_lap("綜觀與召回")

    advisories = _collect_advisories(run, replay_ctx, read_ctx)
    # 函式尾單次寫（dirty-flag 收斂）
    if run.dirty:
        write_state(session_id, state)
    if run.v6 and run.guard:
        _chain_action(state, input_data, config, input_data.get("tool_response"))
    degraded = _hook_lap("寫 state")
    _mark_coding_turn(run)

    # ─── 插件要在 state 落盤之後才看得到的提示 ───
    if not degraded:
        _saved_plugin_hints(run, advisories)

    # 併入的兩支輕檢查（version_guard、驗收規格檔登記）放在 write_state 之後
    degraded = _hook_lap("插件提示")
    sys_msgs = [] if degraded else _run_companion_hooks(input_data, config)
    _hook_lap("同伴檢查")
    _emit_post_tool_output(advisories, sys_msgs)


@dataclass
class _PostRun:
    """一次 PostToolUse 各步共用的材料，開場（_open_run）一次備齊。state 由各步就地改；dirty、aec_reject_msgs 只由欄位旁寫的
    那幾步改；其餘欄位開場後不再變。"""
    input_data: Dict[str, Any]   # 開場：hook 收到的事件
    config: Dict[str, Any]       # 開場
    session_id: str              # 開場
    state: Dict[str, Any]        # 開場：_ensure_state 讀到的 state；各步就地改，主流程尾端單次 write_state
    tool_name: str               # 開場
    tool_input: Dict[str, Any]   # 開場
    file_path: str               # 開場：file_path，NotebookEdit 取 notebook_path
    v6: bool                     # 開場：寫碼守門開關
    guard: bool                  # 開場：守門家族開關（guard_family_on；關＝插件版，只留記憶本體的記帳）
    dirty: bool                  # 各步：改了 state 就設 True（只設不清）；主流程看它決定寫不寫 state
    aec_reject_msgs: List[str]   # 收尾檢核、收割回報兩步追加；收提示那步排在最前面回給模型


def _open_run(input_data: Dict[str, Any], config: Dict[str, Any], session_id: str, state: Dict[str, Any]) -> _PostRun:
    """開場（讀到 state 之後）：這次工具呼叫的名字、輸入、改的檔、寫碼守門開關；dirty 與退回訊息從空開始。"""
    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})
    # NotebookEdit 的路徑欄位是 notebook_path，映射進同一條 modified_files 軌
    file_path = tool_input.get("file_path", "") or tool_input.get("notebook_path", "")

    # dirty-flag 模式：各步的 state 變異只標 run.dirty，主流程尾端單次 write_state。
    # 同一則回覆的平行工具呼叫會同時起好幾支 PostToolUse：read（_ensure_state）到尾端 write 整段握著這個 session 的
    # state 鎖（dispatcher 開的 hook 模式，規則 STATE_RMW_LOCKED），別支排隊，不會互相蓋掉。
    dirty = False
    aec_reject_msgs: List[str] = []   # 收尾檢核、收割回報兩步填；收提示那步排在最前面回給模型
    v6 = v6_enabled(config)           # 關＝只跑寫碼守門之前就有的記帳與提示
    return _PostRun(input_data=input_data, config=config, session_id=session_id, state=state, tool_name=tool_name,
                    tool_input=tool_input, file_path=file_path, v6=v6, guard=guard_family_on(config), dirty=dirty,
                    aec_reject_msgs=aec_reject_msgs)


# ─── 工具記帳 ───────────────────────────────────────────────────────


def _note_every_result(run: _PostRun) -> List[str]:
    """每則工具結果都先記的帳（兩支都在 post_tool_use_reads）：Read 補帳；救援日誌與工具結果體積。
    回補帳的 Read 的提示，收提示那步把它排在這則自己的讀檔提示前面。"""
    state, session_id, config, input_data = run.state, run.session_id, run.config, run.input_data
    tool_name, tool_input, v6 = run.tool_name, run.tool_input, run.v6
    # 規則 READ_FAST_PATH：Read 的 PostToolUse 只落 read-log（dispatcher 快路徑），這裡先把累積的 Read 補帳（提示隨這則工具結果送）
    replay_ctx, replay_dirty = replay_read_log(state, session_id, config, v6)
    run.dirty = run.dirty or replay_dirty

    run.dirty = _note_tool_result(state, session_id, tool_name, tool_input, input_data, config) or run.dirty
    return replay_ctx


def _note_subagent(run: _PostRun) -> None:
    """sub-agent 注入歸因記錄（Agent／Task 工具回來時）；出錯只 stderr。"""
    state, input_data, tool_name = run.state, run.input_data, run.tool_name
    # tool_response 含 agentId / prompt（注入後的完整 prompt），前景子代理另有 content；從 blob marker 回推注入清單
    # + 擷取輸出摘要，keyed by agentId 寫入 state，供注入→使用→結果歸因。背景子代理（async_launched）的結果
    # 由 SubagentStop 回填（規則 SUBAGENT_RESULT_FROM_SUBAGENT_STOP）。
    if tool_name in ("Agent", "Task"):
        try:
            if _record_subagent_injection(state, input_data):
                run.dirty = True
        except Exception as e:
            print(f"sub-agent inject record error: {e}", file=sys.stderr)


def _edited_file_hooks(run: _PostRun) -> None:
    """Edit／Write 改了檔：插件點 PostToolUse.edited；改的是 skills/*/SKILL.md 就背景重生 skill 索引（_maybe_sync_skill_index）。"""
    state, config, session_id = run.state, run.config, run.session_id
    tool_name, file_path = run.tool_name, run.file_path
    if tool_name in ("Edit", "Write") and file_path:
        wg_plugins.emit("PostToolUse.edited", state=state, config=config, session_id=session_id, tool_name=tool_name,
                        file_path=file_path)
        _maybe_sync_skill_index(file_path, config)


def _note_ledgers(run: _PostRun) -> None:
    """殘檔帳本（寫碼守門關）與產出物帳本（寫碼守門開），都在 handlers/aec_ledger；出錯只進 debug log。"""
    state, session_id, input_data = run.state, run.session_id, run.input_data
    tool_name, tool_input, file_path, v6 = run.tool_name, run.tool_input, run.file_path, run.v6
    if not run.guard:   # 兩本帳都是收尾檢核（AEC）的，守門家族關不記
        return
    if not v6 and tool_name in ("Edit", "Write", "NotebookEdit") and file_path:
        try:   # 寫碼守門關時的殘檔帳本：只收寫進系統 tempdir 的檔
            aec_ledger.record_temp_write(session_id, file_path, int(state.get("turn_seq", 0)))
        except Exception as e:
            _atom_debug_error("post_tool_use:aec_ledger_write", e)
    if v6 and tool_name in ("Edit", "Write", "NotebookEdit", "Bash", "PowerShell"):
        # 產出物帳本：這次工具呼叫建立的檔、資料夾、git 工作樹／分支／臨時 repo → 進帳，HUD 現場查還在的列出來。
        try:
            aec_ledger.record_tool_artifacts(
                session_id, tool_name, tool_input,
                input_data.get("cwd", "") or state.get("session", {}).get("cwd", "") or os.getcwd(),
                int(state.get("turn_seq", 0)))
        except Exception as e:
            _atom_debug_error("post_tool_use:aec_ledger_write", e)


def _record_by_tool(run: _PostRun) -> None:
    """依工具種類記帳，一次只走一支：改檔（非暫存路徑）／指令／收尾檢核（MCP 拒收的只印一行）／卡片操作收據／收割回報。
    其他工具這步不記。"""
    state, input_data, tool_name, file_path = run.state, run.input_data, run.tool_name, run.file_path
    _edit_tool = tool_name in ("Edit", "Write", "NotebookEdit", "MultiEdit") and bool(file_path)
    if _edit_tool and _is_ephemeral_path(file_path) and note_ephemeral_skip(state, file_path):   # EPHEMERAL_WRITE_NOTED_ONCE
        run.dirty = True
    if _edit_tool and not _is_ephemeral_path(file_path):
        _record_edit(run)
    elif tool_name in ("Bash", "PowerShell"):
        if run.guard:   # 指令記帳全是寫碼查核、紅測、版控閘用的
            _record_shell(run)
    elif tool_name.endswith("anti_evasion_report") and not run.guard:
        pass            # 收尾檢核（AEC）不出廠：守門家族關不收
    elif tool_name.endswith("anti_evasion_report") and is_error_response(input_data.get("tool_response")):
        # MCP 端拒收（缺三類、g 沒填、harvest_items 形狀不對）的呼叫不是報告：不收、不落檔、不核收割；守門開關都一樣，
        # 不然被退回的那次會被 Stop 當成已交、HUD 畫出缺類的卡
        print(i18n.t("hook.post_tool_use.aec_rejected"), file=sys.stderr)
    elif tool_name.endswith("anti_evasion_report"):
        _record_closing_report(run)
    elif tool_name.endswith("atom_write") or tool_name.endswith("atom_retire"):
        _record_atom_receipt(run)
    elif tool_name.endswith("knowledge_harvest_report"):
        _record_harvest_report(run)


# ─── 改檔（Edit／Write／NotebookEdit／MultiEdit）──────────────────────


def _record_edit(run: _PostRun) -> None:
    """改檔（非暫存路徑）：進 modified_files、改檔次數、重試追蹤、跨 session 改寫偵測；再依路徑跑記憶索引、staging 命名、
    路徑強制、AIDocs 暫時性檔名、DocDrift。"""
    state, session_id, config, input_data = run.state, run.session_id, run.config, run.input_data
    tool_name, file_path, v6 = run.tool_name, run.file_path, run.v6
    _note_modified(state, session_id, file_path, tool_name, str(input_data.get("agent_id") or ""))
    if v6 and run.guard:
        _note_launcher(file_path)       # LAUNCHER_NOT_CODE：worktree 根的一次性 git 啟動器記標記，收尾不當程式算

    edit_counts = state.setdefault("edit_counts", {})
    edit_counts[file_path] = edit_counts.get(file_path, 0) + 1

    try:
        track_retry(state, file_path)
    except Exception as e:
        print(f"Retry track error: {e}", file=sys.stderr)

    try:
        qf = _check_output_quality(file_path, session_id, config)
        if qf:
            state.setdefault("quality_feedback", {}).setdefault(
                "rewritten_files", []
            ).append(qf)
            print(
                f"Quality feedback: {file_path} was also modified "
                f"in session {qf['original_session']}",
                file=sys.stderr,
            )
    except Exception as e:
        print(f"Quality check error: {e}", file=sys.stderr)

    run.dirty = True

    normalized = file_path.replace("\\", "/")
    if "/memory/" in normalized and normalized.endswith(".md"):
        _trigger_incremental_index(config)

    _staging_name_gate(state, normalized)
    _path_enforce_gate(state, normalized)
    _aidocs_gate(state, config, normalized, v6)
    if run.guard:   # DocDrift 屬守門家族
        _docdrift_check(run, normalized)


def _staging_name_gate(state: Dict[str, Any], normalized: str) -> None:
    """staging 命名閘：_staging/ 頂層的 .md 不叫 next-phase*.md 就提示改名（提示先存 state，收提示那步送出）。"""
    if "/_staging/" in normalized and normalized.endswith(".md"):
        # 只檢 _staging/ 頂層檔：子資料夾是多檔工作區（彙整/評估文件），
        # 非 /continue 交接入口，不套 next-phase 命名慣例
        staging_tail = normalized.split("/_staging/", 1)[-1]
        staging_fname = staging_tail.rsplit("/", 1)[-1]
        # 命名慣例＝ `next-phase*.md`（/continue 與 ups_search.continue_handoff_path
        # 掃的同一組檔）：多份計畫並存為常態，
        # 不得要求收斂成單一 next-phase.md。
        if "/" not in staging_tail and not staging_fname.startswith("next-phase"):
            state["_staging_advisory"] = i18n.t("hook.post_tool_use.staging_name", fname=staging_fname)
            print(
                f"Staging name gate: {staging_fname}", file=sys.stderr
            )


def _path_enforce_gate(state: Dict[str, Any], normalized: str) -> None:
    """路徑強制：寫進 ~/.claude/projects/*/memory/ 的檔（MEMORY.md、access.json 除外）提示搬到專案的 .claude/memory/
    （提示先存 state，收提示那步送出）。"""
    _claude_projects_pat = "/.claude/projects/"
    if not (_claude_projects_pat in normalized and "/memory/" in normalized):
        return
    _proj_root = state.get("atom_index", {}).get("project_root", "")
    if not _proj_root:
        return
    _rel_part = normalized.split("/memory/", 1)[-1]
    _exempt = (
        _rel_part == "MEMORY.md"
        or _rel_part == "access.json"
    )
    if _exempt:
        return
    _proj_root_norm = _proj_root.replace("\\", "/")
    _correct_base = f"{_proj_root_norm}/.claude/memory/"
    state["_path_enforcement_advisory"] = i18n.t("hook.post_tool_use.path_enforce", base=_correct_base, rel=_rel_part)
    print(
        f"Path enforcement BLOCKED: {normalized} → should be {_correct_base}{_rel_part}",
        file=sys.stderr,
    )


def _aidocs_gate(state: Dict[str, Any], config: Dict[str, Any], normalized: str, v6: bool) -> None:
    """AIDocs 暫時性檔名閘：_AIDocs/ 底下寫了看起來是暫時性文件的檔名（is_plan_filename）→ 提示改放 memory/_staging/
    （提示先存 state，收提示那步送出；寫碼守門開時同一個檔一個 session 只提醒一次）。"""
    if not ("/_AIDocs/" in normalized or "/_aidocs/" in normalized.lower()):
        return
    fname = normalized.rsplit("/", 1)[-1]
    once = v6 and config.get("aidocs", {}).get("advisory_v6_once", True)   # 寫碼守門開著時同一個檔一個 session 只提醒一次
    if is_plan_filename(fname) and not (once and normalized in state.get("aidocs_advisory_sent", [])):
        state["_aidocs_advisory"] = i18n.t("hook.post_tool_use.aidocs_temp", fname=fname)
        if once:
            state["aidocs_advisory_sent"] = [*state.get("aidocs_advisory_sent", []), normalized]
        print(f"AIDocs gate triggered: {fname}", file=sys.stderr)


def _docdrift_check(run: _PostRun, normalized: str) -> None:
    """DocDrift：改的是 _AIDocs 文件 → 銷掉對應的待更新項（resolve_doc_update）；改的是其他檔 → 有對照的文件就記待更新
    （check_source_drift）。出錯只 stderr。"""
    state, config, file_path = run.state, run.config, run.file_path
    if not (DOCDRIFT_AVAILABLE and config.get("docdrift", {}).get("enabled", True)):
        return
    try:
        if "/_aidocs/" in normalized.lower():
            resolve_doc_update(file_path, state, config)
        else:
            check_source_drift(file_path, state, config)
        run.dirty = True
    except Exception as e:
        print(f"DocDrift error: {e}", file=sys.stderr)


# ─── 指令（Bash／PowerShell）────────────────────────────────────────


def _record_shell(run: _PostRun) -> None:
    """指令：執行回合、版控查詢、改檔目標、搬目錄、commit、紅測、驗證收據。寫碼守門關時只記版控查詢、commit 回合與紅測。"""
    state, session_id, config, input_data = run.state, run.session_id, run.config, run.input_data
    tool_name, tool_input, v6 = run.tool_name, run.tool_input, run.v6
    command = tool_input.get("command", "")
    # 寫碼查核 (c)「說試過／跑了」看本回合有沒有執行過指令：記最後一次執行的 turn_seq（主 session 用 sid、子代理用它自己的收據鍵），
    # 同伴訊息讓 transcript 另起一段之後，同一個使用者回合裡較早跑的指令照樣算（規則 TURN_SINCE_USER_PROMPT）
    if v6:
        _note_exec_turn(run)
    if re.search(r"\b(git\s+(log|blame|show|diff)|svn\s+(log|blame|diff))\b", command):
        vcs = state.setdefault("vcs_queries", [])
        vcs.append({"command": command[:200], "at": _now_iso()})
        run.dirty = True

    # Bash／PowerShell 改檔目標（>、>>、tee、sed -i、cp、mv、Set-Content…）也進 modified_files；
    # 先記目標再落帳，帳本的 modified_files_turn 才含本次寫入；commit 的記帳也在記完之後才做
    # （`sed -i a.py && git commit` 這種同一條指令改檔又 commit，committed_new_code 要找得到 a.py 所在的根）
    if v6 and _write_paths_from_tool is not None:
        _note_shell_writes(run, command)
    # 規則 MOVE_CODE_DIR_NEEDS_SCAN_AND_SAMPLE：記搬了哪個程式目錄（子代理搬的也記，帶 agent_id）、掃描工具的命中清單
    # （Stop 寫碼查核 (v) 用）
    if v6:
        _note_shell_moves(run, command)

    if not v6 and _VCS_COMMIT_RE.search(command):   # 寫碼守門關時只記本回合有 commit（ScanReport 閘豁免收尾檢核）
        state["last_commit_turn_seq"] = int(state.get("turn_seq", 0))
        run.dirty = True
    elif v6:
        if _note_commit(state, command, config, session_id):
            run.dirty = True

    if _track_test_result(state, input_data, command, tool_name, config):
        run.dirty = True
    if v6:
        try:
            if _track_sentinels(state, input_data, command, tool_name, config, session_id):
                run.dirty = True
        except Exception as e:
            print(f"[verify-ledger] error (fail-open): {e}", file=sys.stderr)
            _atom_debug_error("post_tool_use:verify_ledger", e)


def _note_exec_turn(run: _PostRun) -> None:
    """記本回合最後一次執行指令的 turn_seq（state["exec_turn_seq"]，鍵有上限，超量先丟最久沒執行的）。"""
    state, session_id, input_data = run.state, run.session_id, run.input_data
    _exec_aid = str(input_data.get("agent_id") or "")
    _exec = state.setdefault("exec_turn_seq", {})
    _exec_key = _vl.agent_receipt_key(session_id, _exec_aid) if _exec_aid else session_id
    _exec.pop(_exec_key, None)                                          # 重新插入排到最後，超量時先丟最久沒執行的
    _exec[_exec_key] = int(state.get("turn_seq", 0))
    for _old in list(_exec)[:-EXEC_TURN_KEYS_CAP]:
        _exec.pop(_old, None)
    run.dirty = True


def _note_shell_writes(run: _PostRun, command: str) -> None:
    """指令的改檔目標進 modified_files；目標不明的記未知寫入；暫存檔不記帳。出錯只進 debug log（這一批後面的目標就不記了）。"""
    state, session_id, input_data = run.state, run.session_id, run.input_data
    tool_name, tool_input = run.tool_name, run.tool_input
    try:
        _cwd = input_data.get("cwd", "") or state.get("session", {}).get("cwd", "") or ""
        _aid = str(input_data.get("agent_id") or "")
        for _p in _write_paths_from_tool(tool_name, tool_input, _cwd) or []:
            if _p and _p.startswith("UNKNOWN:"):    # 目標不明：記未知寫入，再從版控狀態補出真的改了哪些檔
                _note_unknown_write(state, session_id, _p, command, tool_name, _aid, input_data)
            elif _p and not _is_ephemeral_path(_p):
                _note_modified(state, session_id, _p, tool_name, _aid)
                _note_launcher(_p)        # LAUNCHER_NOT_CODE：用 shell 寫出來的啟動器也認；刪掉時標記不動
            elif not (_p and note_ephemeral_skip(state, _p)):    # 暫存檔不記帳；暫存目錄專案第一次說一聲（EPHEMERAL_WRITE_NOTED_ONCE）
                continue
            run.dirty = True
    except Exception as e:
        _atom_debug_error("post_tool_use:write_paths_from_tool", e)


def _note_shell_moves(run: _PostRun, command: str) -> None:
    """記搬了哪個程式目錄、掃描工具的命中清單；出錯只 stderr。"""
    state, session_id, config, input_data = run.state, run.session_id, run.config, run.input_data
    tool_name, tool_input = run.tool_name, run.tool_input
    try:
        _mv_cwd = input_data.get("cwd", "") or state.get("session", {}).get("cwd", "") or ""
        if _note_moved_code_dirs(state, session_id, tool_name, tool_input, _mv_cwd, config,
                                 agent_id=str(input_data.get("agent_id") or "")):
            run.dirty = True
        if _note_move_scan_hits(state, command, input_data.get("tool_response"), _mv_cwd):
            run.dirty = True
    except Exception as e:  # noqa: BLE001
        print(i18n.t("hook.post_tool_use.move_fact_failed", error=f"{type(e).__name__}: {e}"), file=sys.stderr)


# ─── 收尾檢核（anti_evasion_report）─────────────────────────────────


def _record_closing_report(run: _PostRun) -> None:
    """收尾檢核四段：組報告 → 收割清單與欠寫項 → 證據路徑與追蹤項 → 落 state 與報告檔。"""
    # MCP 結構化收尾 emit（one-writer spine）：MCP tool 只回 chip、不碰 state；
    # 由此 PostToolUse 分支獨佔寫 state + 落 per-turn 報告檔 + 判 HUD fallback。
    # session_id 用原始 input_data["session_id"]（與 modified_files 的 session_id 戳
    # 同源）＝sibling 隔離關鍵：Stop 閘以 turn_seq+session_id 雙鍵讀，隔壁 session 的
    # emit 不誤放行本 session。turn_seq 由 UserPromptSubmit 每真 prompt +1。
    report, vals, sev, turn_seq, i_paths = _aec_build_report(run)
    _aec_owed_items(run, report, vals, turn_seq)
    _aec_evidence_fields(run, report, vals)
    _aec_save(run, report, sev, turn_seq, i_paths)
    run.dirty = True


def _aec_build_report(run: _PostRun) -> Tuple[Dict[str, Any], Dict[str, str], str, int, str]:
    """收尾檢核①組報告：照收各欄、(i) 的「無」正規化、算嚴重度再拿 hook 實測的退避證據 cross-check、三類、「你說」核對；
    升級時把證據寫進 (b)。回 (report, vals, severity, turn_seq, i_paths)。"""
    state, session_id, input_data, tool_input, v6 = run.state, run.session_id, run.input_data, run.tool_input, run.v6
    # (c) 不收（與 85% 預警重複），舊客戶端送來不存；e／f 不在 schema（內容歸 ai_decided），舊客戶端送來照存、HUD 照舊格式畫
    _AEC_KEYS = ("a", "b", "d", "e", "f", "g", "h", "i")
    vals = {k: str(tool_input.get(k, "") or "") for k in _AEC_KEYS}
    # (j)(k)：鍵不存在＝舊格式呼叫（MCP 還沒載到新 schema），不寫進報告、Stop 也不拿它擋；明給空字串才擋
    for _k in ("j_evidence_path", "k_followups") if v6 else ():
        if _k in tool_input:
            vals[_k] = str(tool_input.get(_k) or "")
    a, b = vals["a"], vals["b"]
    # (i) 是給使用者裁決的路徑清單：「無（…括號解釋…）」一律正規化成目前語言的「無」，
    # 模型的多嘴說明（執行期狀態檔、已刪了什麼）不得變成 HUD 上要人裁決的一列。
    if _aec_blank(vals["i"]):
        vals["i"] = i18n.t("hook.post_tool_use.none")
    i_paths = vals["i"]
    turn_seq = int(state.get("turn_seq", 0))
    sev = aec_severity(a, b)
    # (b) 欄 cross-check：hook 實測到退避但模型自評「無」→ 升 real-evasion +
    # 附 hook 證據（升級只發生在 Python one-writer；Node chip 純內容判定，
    # 顯示可能不同步——report 檔 + Stop fallback 為準）。
    evidence = _collect_aec_evidence(state, session_id)
    sev, upgraded = crosscheck_aec_severity(sev, b, evidence)
    report = {
        "session_id": session_id,
        "turn_seq": turn_seq,
        **vals,
        "severity": sev,
        "at": _now_iso(),
    }
    # 三類（本 session 目標／大原則方向／AI 自己做主）給 HUD 分區畫：形狀由 MCP 端拒收檢查管，這裡只照收
    # （留 dict 項、只留認得的鍵、值轉字串），不判、不影響 severity。沒給（舊客戶端）就不寫，HUD 照舊格式畫。
    from handlers.post_tool_use_aec import _AEC_CLASS_KEYS
    for _cls, _keys in _AEC_CLASS_KEYS.items():
        _raw = tool_input.get(_cls)
        if isinstance(_raw, list):
            report[_cls] = [{k: str(it.get(k) or "") for k in _keys if k in it} for it in _raw if isinstance(it, dict)]
    # 「你說」核對：goal[].said 拿主對話紀錄裡使用者本人打的字比，每項加 said_check、另存本回合原話給 HUD 對照。
    # 只加記號、不擋、不改 severity；守門開關都跑；讀不到紀錄標 unreadable ＋ stderr。
    if isinstance(report.get("goal"), list):
        from handlers import aec_said_check
        aec_said_check.annotate(report, input_data)
    if upgraded:
        report["severity_upgraded_by"] = "hook:evasion-crosscheck"
        report["hook_evidence"] = evidence[-5:]
        # (b) 卡片只渲染 b 欄；升級卻留「無」會出現「紅框指著空卡」（可觀測性
        # 鐵律：訊號必須帶內容浮出）。把 hook 證據寫進 b，模型原自評另存 b_model。
        report["b_model"] = b
        ev_lines = i18n.t("hook.stop.evidence_sep").join(
            f"turn {e.get('turn_seq', '?')}『{e.get('phrase', '')}』"
            for e in evidence[-5:] if e.get("phrase")
        )
        report["b"] = i18n.t("hook.post_tool_use.b_crosscheck", b=b or i18n.t("hook.post_tool_use.none"),
                             count=len(evidence), evidence=ev_lines)
    return report, vals, sev, turn_seq, i_paths


def _aec_owed_items(run: _PostRun, report: Dict[str, Any], vals: Dict[str, str], turn_seq: int) -> None:
    """收尾檢核②收割清單（harvest_items）、白話全文（report）、欠寫項 (d)(h)：欠著的落 d_pending 並回告模型。"""
    state, session_id, input_data, tool_input, v6 = run.state, run.session_id, run.input_data, run.tool_input, run.v6
    aec_reject_msgs = run.aec_reject_msgs
    # (d)/(h) pending：把「記憶寫入」推到之後（尚未寫／見下一動／下一動＝寫 atom）。
    # 報告是收尾檢核，不是待辦清單——落 d_pending 供 HUD 標紅，並回告模型當回合補寫；
    # Stop 端讀 d_pending 擋一次（AEC-Pending Gate），逼 atom_write 後重新 emit。
    # 本 turn 已有 validated 收割 → (d) 的記憶收錄帳已由收割核對過，不再判 d_pending；(h) 照舊。
    # 收尾一份報告（CLOSING_REPORT_ONE_CALL）：收割清單可以跟收尾檢核同一次交（harvest_items，核對同 knowledge_harvest_report）；
    # report＝給讀者的白話全文（做了什麼／驗了什麼／沒做什麼／追蹤項）。
    if v6 and isinstance(tool_input.get("harvest_items"), list):
        _apply_harvest(state, session_id, input_data, tool_input["harvest_items"], "", aec_reject_msgs,
                       "anti_evasion_report (harvest_items)")
    if v6 and str(tool_input.get("report") or "").strip():
        report["report"] = str(tool_input["report"])
    hv_done = validated_this_turn(state, session_id, turn_seq)
    pending = aec_pending_items("" if hv_done else vals["d"], vals["h"])
    # (d) 只寫「見收割報告」：本使用者回合要真的交過核對過的收割報告，否則這份記憶收錄帳等於沒寫
    if v6 and not hv_done and re.match(r"^\s*(?:見收割報告|see\s+harvest\s+report)", vals["d"], re.IGNORECASE):
        pending.insert(0, i18n.t("hook.post_tool_use.d_see_harvest"))
    if pending:
        report["d_pending"] = pending
        aec_reject_msgs.append(
            i18n.t("hook.post_tool_use.aec_pending", count=len(pending), items="\n".join(f"  ✗ {x}" for x in pending))
        )


def _aec_evidence_fields(run: _PostRun, report: Dict[str, Any], vals: Dict[str, str]) -> None:
    """收尾檢核③ (j) 證據持久路徑（有驗證收據時要給真存在的路徑，留空就帶入帳本）與 (k) 追蹤項；缺的回告模型。"""
    state, session_id = run.state, run.session_id
    aec_reject_msgs = run.aec_reject_msgs
    # (j) 證據持久路徑：本 session 有驗證收據時必須給真存在的路徑；模型留空／「無」而帳本在 → 系統帶入帳本絕對路徑
    # （帳本在資料根，跟專案 cwd 不同根）。(k) 追蹤項：空字串不收（「無」可）。
    # 判定落 report 供 HUD 標紅；Stop 的對帳閘 (e) 讀 report 裡（帶入後）的 j，以同一對函式擋。
    if "j_evidence_path" in vals:
        _has_receipts = bool(_vl.receipts_of(state, session_id))
        _ledger = str(_vl.ledger_path(session_id, WORKFLOW_DIR))
        _j = _vl.aec_evidence_autofill(vals["j_evidence_path"], _has_receipts, _ledger)
        if _j != vals["j_evidence_path"]:
            vals["j_evidence_path"] = report["j_evidence_path"] = _j
            report["j_autofilled"] = True
            aec_reject_msgs.append(i18n.t("hook.post_tool_use.aec_evidence_autofill", path=_j))
        report["j_missing"] = _vl.aec_evidence_missing(_j, _has_receipts)
    if "k_followups" in vals:
        report["k_missing"] = _vl.aec_followups_missing(vals["k_followups"])
    if report.get("j_missing"):
        aec_reject_msgs.append(
            i18n.t("hook.post_tool_use.aec_evidence_missing", ledger=_vl.ledger_path(session_id, WORKFLOW_DIR))
        )
    if report.get("k_missing"):
        aec_reject_msgs.append(i18n.t("hook.post_tool_use.aec_followups_empty"))


def _aec_save(run: _PostRun, report: Dict[str, Any], sev: str, turn_seq: int, i_paths: str) -> None:
    """收尾檢核④落 state 與 per-turn 報告檔、(i) 衍生暫存進殘檔帳本（受保護路徑拒收並回告）、插件點 AecReport.saved
    （沒插件看得到又有退避訊號 → 標 aec_hud_fallback）。"""
    state, session_id, config, input_data = run.state, run.session_id, run.config, run.input_data
    aec_reject_msgs = run.aec_reject_msgs
    state["anti_evasion_report"] = report
    _write_aec_report_file(session_id, turn_seq, report)
    # 殘檔帳本：(i) 一行一路徑宣告 + session scratchpad 掃描 → 進帳（HUD 讀帳本 + exists()）。
    # (i) 裡的受保護路徑（VCS 追蹤檔 / memory、_AIDocs / 索引類）拒收並回告模型——
    # 「已改未 commit 的正式檔」屬 (a)(b)/(g) 未同步事項，不是衍生暫存；靜默丟掉會讓模型一直錯報。
    try:
        _cwd = state.get("session", {}).get("cwd", "") or input_data.get("cwd", "")
        _rejected: List[Dict[str, str]] = []
        aec_ledger.collect_at_completion(session_id, _cwd, i_paths, turn_seq, rejected=_rejected)
        if _rejected:
            _protected = any(not r["path"].startswith("branch:") for r in _rejected)
            aec_reject_msgs.append(
                i18n.t("hook.post_tool_use.aec_ledger_rejected",
                       items="\n".join(f"  ✗ {r['path']} — {r['reason']}" for r in _rejected),
                       protected=i18n.t("hook.post_tool_use.aec_ledger_protected") if _protected else "")
            )
    except Exception as e:
        _atom_debug_error("post_tool_use:aec_ledger_collect", e)
    # 報告怎麼給人看是插件的事；沒有插件回 True（看得到）又有退避訊號 → 標旗，Stop 印一行報告檔在哪
    seen = any(wg_plugins.emit("AecReport.saved", severity=sev, state=state, config=config, session_id=session_id))
    if not seen and sev in ("notable", "real-evasion"):
        state["aec_hud_fallback"] = True


# ─── 卡片操作收據、收割回報 ─────────────────────────────────────────


def _record_atom_receipt(run: _PostRun) -> None:
    """卡片操作收據（atom_write／atom_retire）入帳；atom_write 再補來源。"""
    state, session_id, input_data, tool_name = run.state, run.session_id, run.input_data, run.tool_name
    # atom 工具成功時結果最後一行 `receipt: {json}`（失敗呼叫沒有 receipt → 不記）。
    # 入帳 state["atom_ops"][sid]，收割回報用它逐項核對（exists()/全域 resolver 驗不出
    # 專案層 atom、append 是否真發生、索引是否成功；receipt 可以）。
    receipt = parse_receipt(input_data.get("tool_response"))
    if receipt:
        record_atom_op(state, session_id, int(state.get("turn_seq", 0)), receipt)
        run.dirty = True
        if tool_name.endswith("atom_write"):
            # 來源自動填：剛建／改的 atom 補 Source／Quote（失敗只記 log，不阻斷）
            from wg_provenance import autofill_from_receipt
            autofill_from_receipt(receipt, input_data, state)


def _record_harvest_report(run: _PostRun) -> None:
    """收割回報（knowledge_harvest_report）：核對後落本 session 分區與帳；MCP 拒收的只印一行。"""
    state, session_id, input_data, tool_input = run.state, run.session_id, run.input_data, run.tool_input
    aec_reject_msgs = run.aec_reject_msgs
    # 收割回報（one-writer）：items ↔ 本 session 自上次 validated 收割以來的 receipts 逐項核對，
    # 落本 session 分區 + ledger；核不過 → pending（Stop 的 Harvest-Pending 閘擋一次要求補）。
    # MCP 端已拒收（isError）的呼叫不是回報：不核對、不落分區、不 spawn，否則拒收的 items
    # 會被當成 validated 放行。
    if is_error_response(input_data.get("tool_response")):
        print(i18n.t("hook.post_tool_use.harvest_rejected"), file=sys.stderr)
    else:
        _apply_harvest(state, session_id, input_data, tool_input.get("items"), tool_input.get("note", ""), aec_reject_msgs,
                       "knowledge_harvest_report")
        run.dirty = True


# ─── 工具記帳之後：清帳、綜觀與召回、收提示、寫 state 之後 ─────────────


def _prune_docdrift(run: _PostRun) -> None:
    """DocDrift 清帳：來源檔已經不髒（commit 或還原）的待更新項拿掉；降級後也照做。出錯只進 debug log。"""
    state, config = run.state, run.config
    if not (run.guard and DOCDRIFT_AVAILABLE and config.get("docdrift", {}).get("enabled", True)):
        return
    try:
        if prune_committed_entries(state, config) > 0:
            run.dirty = True
    except Exception as e:
        _atom_debug_error("post_tool_use:docdrift_prune", e)
        pass


def _overview_and_recall(run: _PostRun, degraded: bool) -> Tuple[List[str], bool]:
    """綜觀與召回（post_tool_use_reads._read_tracking）：讀檔事件的 OverviewHub 讀檔帳與 Depends 召回，裡面打「讀檔帳」那個點，
    降級後召回不跑。回（這則自己的讀檔提示、打完點後的降級旗標）。"""
    state, session_id, config, input_data = run.state, run.session_id, run.config, run.input_data
    tool_name, tool_input, v6 = run.tool_name, run.tool_input, run.v6
    read_ctx, _rd, degraded = _read_tracking(state, session_id, tool_name, tool_input, input_data, config, v6, degraded, _hook_lap)
    run.dirty = run.dirty or _rd
    return read_ctx, degraded


def _collect_advisories(run: _PostRun, replay_ctx: List[str], read_ctx: List[str]) -> List[str]:
    """收提示（給模型的 additionalContext），照這個先後：收尾檢核／收割回報的退回訊息、補帳的 Read 的提示、這則自己的讀檔提示、
    工具前守門判成提醒的那幾件、前面各閘暫存在 state 的一次性提示。"""
    state, v6 = run.state, run.v6
    aec_reject_msgs = run.aec_reject_msgs
    advisories = list(aec_reject_msgs)
    advisories.extend(replay_ctx)       # 補帳的 Read 的提示（含上一個 PreToolUse 補出來、存在 state 裡的）排在這則自己的前面
    advisories.extend(read_ctx)
    # 提醒（規則 REMIND_ON_PRIOR_RESULT、REMIND_PENDING_IN_STATE）：工具前守門判成提醒的那幾件，在這則工具結果送一行（同規則同對象一 session 一次）
    if v6 and run.guard:
        _flush_reminders(run, advisories)
    if state:
        _drain_state_advisories(run, advisories)
    return advisories


def _flush_reminders(run: _PostRun, advisories: List[str]) -> None:
    """提醒：工具前守門判成提醒、存在 state 的那幾件，併進這則工具結果的提示；出錯只 stderr。"""
    state, session_id = run.state, run.session_id
    try:
        from wg_overview import remind_flush
        if state.get("remind_pending"):
            run.dirty = True
        remind_lines = remind_flush(state, session_id)
        advisories.extend(remind_lines)
    except Exception as e:  # noqa: BLE001
        print(i18n.t("hook.post_tool_use.remind_failed", error=f"{type(e).__name__}: {e}"), file=sys.stderr)


def _drain_state_advisories(run: _PostRun, advisories: List[str]) -> None:
    """前面各閘暫存在 state 的一次性提示（路徑強制、AIDocs、staging 命名、DocDrift、工具結果體積）加上標籤併進提示，從 state 刪掉。"""
    state = run.state
    for key, prefix in [
        ("_path_enforcement_advisory", "[Guardian:PathEnforce]"),
        ("_aidocs_advisory", "[Guardian:AIDocs]"),
        ("_staging_advisory", "[Guardian:StagingName]"),
        ("_docdrift_advisory", "[Guardian:DocDrift]"),
        ("_tool_result_advisory", "[Guardian:ToolResultSize]"),
    ]:
        val = state.get(key)
        if val:
            advisories.append(f"{prefix} {val}" if prefix else str(val))
            del state[key]
            run.dirty = True


def _mark_coding_turn(run: _PostRun) -> None:
    """寫碼守門開時：寫碼回合就建標記（PreToolUse 看它），不是就讓這次 hook 結束時不寫計時帳。"""
    state, config, session_id, v6 = run.state, run.config, run.session_id, run.v6
    if v6 and not run.guard:                     # 守門家族關：沒有寫碼回合可判（判法在守門檔），照非寫碼回合不寫計時帳
        import wg_hook_timing
        wg_hook_timing.quiet()
    elif v6:                                     # 規則 CODING_TURN_ONLY：寫碼回合才寫計時帳；PreToolUse 看這裡建的標記
        import wg_hook_timing
        from wg_core import coding_turn
        if coding_turn(state, config):
            wg_hook_timing.mark_coding(session_id, True)
        else:
            wg_hook_timing.quiet()


def _saved_plugin_hints(run: _PostRun, advisories: List[str]) -> None:
    """插件點 PostToolUse.saved：插件在 state 落盤之後才看得到的提示，併進提示、也印到 stderr。"""
    config, session_id, tool_name, file_path = run.config, run.session_id, run.tool_name, run.file_path
    for _msg in wg_plugins.emit("PostToolUse.saved", config=config, session_id=session_id, tool_name=tool_name,
                                file_path=file_path):
        advisories.append(_msg)
        print(_msg, file=sys.stderr)
