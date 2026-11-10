"""
handlers/post_tool_use_reads.py — 讀檔帳與 Read 補帳

從 post_tool_use.py 拆出。做什麼：每個工具結果都記的兩筆帳（救援日誌、工具結果體積）、讀檔事件的 OverviewHub 讀檔帳與 Depends 召回；
Read 事件只落 read-log（規則 READ_FAST_PATH），在下一個事件（或下一個 PreToolUse）把累積的 Read 逐筆補帳。
誰叫它：post_tool_use.handle_post_tool_use；pre_tool_use 與 session_end 叫 replay_reads_before_tool、replay_read_log（經 post_tool_use 轉出）。
"""

import sys
from typing import Any, Dict, List, Optional, Tuple

import i18n   # lib/：給人看的提示走語言表
from wg_core import _ensure_state, write_state, WORKFLOW_DIR, v6_enabled, guard_family_on
from wg_lazy_re import lazy_module
import wg_read_fast  # 規則 READ_FAST_PATH：Read 事件只落 read-log，下一個事件在這裡補帳

_vl = lazy_module("wg_verify_ledger")


def _read_exit_code(tool_response: Any) -> Optional[int]:
    """讀檔用的結束碼：dict 回應只信 exit_code／returncode 欄（stdout 是被讀的檔內容，裡面一行「Exit code 1」不能當失敗）；
    字串回應（實機非零 exit 時整包是 "Error: Exit code N…"）照 exit_code_of 看文字。"""
    if isinstance(tool_response, dict):
        return _vl.exit_code_of({k: tool_response.get(k) for k in ("exit_code", "returncode")})
    return _vl.exit_code_of(tool_response)


def _note_tool_result(state: Dict[str, Any], session_id: str, tool_name: str, tool_input: Dict[str, Any],
                      input_data: Dict[str, Any], config: Dict[str, Any]) -> bool:
    """每個工具結果都做的兩筆帳：救援日誌（工具呼叫命中已注入 atom 的高特異 token → rescue-log）、工具結果體積
    （每筆量長度落 per-session 檔；單筆超門檻 → advisory 存 state）。回 state 有沒有變。"""
    dirty = False
    try:
        from wg_rescue import check_rescue_hits
        if check_rescue_hits(state, session_id, tool_name, tool_input):
            dirty = True
    except Exception as e:
        print(f"rescue check error: {e}", file=sys.stderr)
    try:
        from wg_friction import record_tool_result
        _trs_adv = record_tool_result(
            state, session_id, tool_name, tool_input, input_data.get("tool_response"), config,
            base_dir=WORKFLOW_DIR,
        )
        if _trs_adv:
            state["_tool_result_advisory"] = _trs_adv
            dirty = True
    except Exception as e:
        print(f"tool result size error: {e}", file=sys.stderr)
    return dirty


def _read_tracking(state: Dict[str, Any], session_id: str, tool_name: str, tool_input: Dict[str, Any],
                   input_data: Dict[str, Any], config: Dict[str, Any], v6: bool, degraded: bool,
                   lap: Any) -> Tuple[List[str], bool, bool]:
    """讀檔事件的兩段（Read／Bash／PowerShell／Grep；非讀檔工具只打「讀檔帳」那個點）：OverviewHub 讀檔帳、
    Depends 召回。主流程傳 _hook_lap；read-log 補帳（replay_read_log）傳不打點的 lap。
    回（要附進工具結果的提示、state 有沒有變、打完點後的 degraded）。"""
    ctx: List[str] = []
    dirty = False
    cwd = input_data.get("cwd", "") or ""
    # OverviewHub 讀檔帳：讀到的檔記進各部位的 part_files_read（提問前先讀過部位檔照這筆判；fail-open）
    if not degraded and guard_family_on(config) and tool_name in (("Read", "Bash", "PowerShell", "Grep") if v6 else ("Read", "Bash")):   # Grep：規則 GREP_CONTENT_IS_READ（wg_overview._read_paths）
        try:
            from wg_overview import record_reads
            record_reads(state, tool_name, tool_input, cwd, config,
                         **({"exit_code": _read_exit_code(input_data.get("tool_response"))} if v6 else {}))   # 規則 BASH_READONLY_COUNTS_AS_READ：失敗的讀不算
            dirty = True
        except Exception as e:
            print(f"overview hub read error: {e}", file=sys.stderr)

    degraded = lap("讀檔帳")

    # Depends 召回（A3.5）：讀到的檔若是某張卡的壞滅緣（索引 depends path:），一行一卡指標；文件／計畫檔命中 authority_map 一次性注入地圖
    if v6 and not degraded and tool_name in ("Read", "Bash", "PowerShell"):
        try:
            from wg_depends import on_read_depends as _on_read_depends
            dep_ctx = _on_read_depends(state, session_id, tool_name, tool_input, cwd, config)
            if dep_ctx:
                ctx.append(dep_ctx)
                dirty = True   # depends_hints／authority_map_injected 要落盤
        except Exception as e:
            print(f"depends recall error: {e}", file=sys.stderr)
    return ctx, dirty, degraded


def replay_read_log(state: Dict[str, Any], session_id: str, config: Dict[str, Any], v6: bool) -> Tuple[List[str], bool]:
    """規則 READ_FAST_PATH：Read 的 PostToolUse 走 dispatcher 快路徑只落 read-log；這裡在下一個非 Read 事件把累積的 Read
    逐筆交給 Read 原本會跑的那幾段（工具結果體積、救援命中、OverviewHub 讀檔帳、Depends 召回）
    ——結果一樣、晚一個事件出現。寫碼守門關：Read 本來就不進 handler（dispatcher PRE_V6_TOOLS），整批丟掉。
    回（要附進這則工具結果的提示＝上一個 PreToolUse 補出來存在 state 的＋這次補出來的、state 有沒有變）。"""
    ctx: List[str] = list(state.pop(wg_read_fast.HINTS_KEY, None) or [])
    dirty = bool(ctx)
    if not session_id:
        return ctx, dirty
    entries = wg_read_fast.drain(session_id)
    if not entries or not v6:
        return ctx, dirty
    for ev in (wg_read_fast.event_of(e) for e in entries):
        ti = ev["tool_input"]
        dirty = _note_tool_result(state, session_id, "Read", ti, ev, config) or dirty
        rc, rd, _ = _read_tracking(state, session_id, "Read", ti, ev, config, v6, False, lambda seg: False)
        ctx.extend(rc)
        dirty = dirty or rd
    return ctx, dirty


def replay_reads_before_tool(input_data: Dict[str, Any], config: Dict[str, Any]) -> None:
    """PreToolUse（Edit／Write／Bash… 要讀檔證據的閘之前）：有累積的 Read 就讀 state 補帳、寫回；補出來的提示存
    state[read_replay_hints]，下一個 PostToolUse 隨工具結果送。沒累積就什麼都不碰（不多讀 state）。炸了不擋工具、stderr 出聲。"""
    sid = str(input_data.get("session_id") or "")
    if not sid or not wg_read_fast.pending(sid):
        return
    try:
        state = _ensure_state(sid, input_data, config)
        if not state:
            print(i18n.t("hook.post_tool_use_reads.state_unreadable"), file=sys.stderr)
            return
        ctx, dirty = replay_read_log(state, sid, config, v6_enabled(config))
        if ctx:
            state[wg_read_fast.HINTS_KEY] = ctx
            dirty = True
        if dirty:
            write_state(sid, state)
    except Exception as e:  # noqa: BLE001
        print(i18n.t("hook.post_tool_use_reads.replay_failed", error=f"{type(e).__name__}: {e}"), file=sys.stderr)
