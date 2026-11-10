"""
handlers/pre_tool_use.py — PreToolUse hook 入口

一次工具呼叫依序過這些閘（每道閘一支 pre_tool_use_<事>.py，細節看各檔檔頭）：
  讀檔補帳 → 連鎖停手（chain）→ 提問閘（ask）→ MCP 改卡直接放行 → 子代理記憶注入 → feedback 路由提示（atom_format）
  → 插件提示 → 記憶卡格式（atom_format）→ 記憶路徑／跨 realm 寫入 → svn 測試路徑閘（svn）→ 合併驅動（merge_driver）
  → svn 中文路徑提醒（svn）→ 口令閘（commit_order）→ 驗證批次閘 → 隱私閘（privacy）→ 病形閘（smell）→ OverviewHub 寫入檢查（hub）
  → 連鎖快照（chain）→ 輸出。
deny 一律走 pre_tool_use_chain._deny；裁判子 session 只走 handle_judge_pre_tool_use。
"""

import sys
from typing import Any, Dict, List, Tuple

from wg_core import (
    output_json, output_nothing,
    check_memory_path_block,
    check_cross_realm_write, check_cross_realm_mcp_cmd, check_cross_realm_bash,
    read_state, read_state_status,
    _atom_debug_log, v6_enabled, guard_family_on,
)
import i18n   # lib/：給人看的提示走語言表
import wg_plugins
import wg_read_fast  # 規則 READ_FAST_PATH：改檔前先把累積的 Read 補帳
from wg_lazy_re import lazy_module
_rr = lazy_module("wg_release_rules")       # 擋或提醒（gate_mode／RULE_MODE，規則 GATE_MODE_TABLE）；口令閘的子句切法也在這裡
# 分段計時只打點、不看回傳：逾時被 Claude Code 取消本來就放行，提早放行只會少查（降級不改判定）
from wg_hook_timing import lap as _hook_lap
from handlers.pre_tool_use_atom_format import _check_feedback_routing_advisory, _check_memory_atom_format
from wg_lazy_re import lazy_func
# 守門家族的閘（guard_family_on 關＝插件版，這些檔不出廠）：名字留在模組上給測試換，呼叫時才載
_ask_gate = lazy_func("handlers.pre_tool_use_ask", "_ask_gate")
_chain_before_write = lazy_func("handlers.pre_tool_use_chain", "_chain_before_write")
_chain_halt = lazy_func("handlers.pre_tool_use_chain", "_chain_halt")
check_git_commit_order = lazy_func("handlers.pre_tool_use_commit_order", "check_git_commit_order")
_pre_v6_hub_warn = lazy_func("handlers.pre_tool_use_hub", "_pre_v6_hub_warn")
_remind_later = lazy_func("handlers.pre_tool_use_hub", "_remind_later")
check_merge_driver = lazy_func("handlers.pre_tool_use_merge_driver", "check_merge_driver")
check_git_privacy = lazy_func("handlers.pre_tool_use_privacy", "check_git_privacy")
_smell_once_per_turn = lazy_func("handlers.pre_tool_use_smell", "_smell_once_per_turn")
check_smell_gate = lazy_func("handlers.pre_tool_use_smell", "check_smell_gate")
check_svn_encoding = lazy_func("handlers.pre_tool_use_svn", "check_svn_encoding")
check_svn_test_block = lazy_func("handlers.pre_tool_use_svn", "check_svn_test_block")
# 其他核心程式從這裡取的名字（wg_verify_ledger 用）：本體在 pre_tool_use_shell
_resolve_run_cwd = lazy_func("handlers.pre_tool_use_shell", "_resolve_run_cwd")
_vcs_segments = lazy_func("handlers.pre_tool_use_shell", "_vcs_segments")


def _deny(input_data: Dict[str, Any], config: Dict[str, Any], reason: str) -> None:
    """PreToolUse 的 deny 出口：守門家族開 → 交 pre_tool_use_chain._deny（寫碼守門開時先給連鎖斷路器記一筆）；
    關（插件版）→ 直接送 deny（連鎖斷路器不出廠）。"""
    if guard_family_on(config):
        from handlers.pre_tool_use_chain import _deny as _chain_deny
        _chain_deny(input_data, config, reason)
        return
    output_json({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                        "permissionDecisionReason": reason}})


# sub-agent 注入 budget（緊湊，守 token 紅線；2-3 顆最高活化）
_SUBAGENT_INJECT_BUDGET = 700


def pick_subagent_memory(task_text: str, already_injected: List[str]) -> Tuple[str, List[str]]:
    """子代理要帶的記憶卡：依任務文字挑卡，預算 _SUBAGENT_INJECT_BUDGET，回 (blob, 卡名)；沒挑到回 ("", [])。
    PreToolUse(Agent/Task) 把 blob 塞在 prompt 前面，SubagentStart（handlers/subagent_start）走 additionalContext，挑法同一支。"""
    from wg_atoms import build_injection_blob   # 惰性 import：wg_atoms 全額 import 成本高，只在派子代理時付
    blob, injected = build_injection_blob(task_text, budget=_SUBAGENT_INJECT_BUDGET, already_injected=already_injected)
    return (blob, injected) if blob and injected else ("", [])


def handle_judge_pre_tool_use(input_data: Dict[str, Any], config: Dict[str, Any]) -> None:
    """裁判子 session（CLAUDE_COMPANION_JUDGE=1）的 PreToolUse：只跑「寫入目標判不出 → 放行並留帳 judge_write」這一道
    （與主 session 的 OverviewHub 同一個判定：wg_core.write_paths_from_tool）；其他閘、注入、state 一律不碰（防裁判觸發裁判）。"""
    tool_name = input_data.get("tool_name", "") or ""
    if tool_name not in ("Bash", "PowerShell"):
        output_nothing()
        return
    from wg_core import write_paths_from_tool
    unknown = [t for t in write_paths_from_tool(tool_name, input_data.get("tool_input") or {}, input_data.get("cwd") or None)
               if t.startswith("UNKNOWN:")]
    if unknown:              # 規則 UNKNOWN_TARGET_LOGGED：判不出就放行並留帳
        from wg_core import append_guard_log
        append_guard_log("judge_write", {"event": "unknown_target", "session_id": str(input_data.get("session_id") or ""),
                                         "target": unknown[0][:120], "allowed": True})
    # 驗證批次閘裁判也適用（不碰 state：沒有收據與擋次帳，只判整組驗證要口令；裁判沒有使用者原話，整組一律擋）
    import wg_verify_batch
    deny_reason = wg_verify_batch.check(tool_name, input_data.get("tool_input") or {}, config, None,
                                        input_data.get("cwd") or "", str(input_data.get("session_id") or ""))
    if deny_reason:
        output_json({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                            "permissionDecisionReason": deny_reason}})
        return
    output_nothing()


def handle_pre_tool_use(input_data: Dict[str, Any], config: Dict[str, Any]) -> None:
    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})
    v6 = v6_enabled(config)     # 關＝只跑寫碼守門之前就有的閘（dispatcher 已擋掉寫碼守門才掛的工具）
    guard = guard_family_on(config)   # 關（插件版）＝守門家族的閘一道都不跑，只留子代理注入與導回 atom_write 的寫入閘

    # 規則 READ_FAST_PATH：Read 的 PostToolUse 只落 read-log；要讀檔證據的閘（提問前讀過部位檔）
    # 之前先把累積的 Read 補帳（沒累積只多一次 stat）
    if v6 and guard and wg_read_fast.pending(str(input_data.get("session_id") or "")):
        from handlers.post_tool_use import replay_reads_before_tool
        replay_reads_before_tool(input_data, config)

    # 連鎖斷路器：警報後、使用者選繼續或放行之前，讀與問以外的工具先停手（不是閘觸發、不進次數）
    _halt = _chain_halt(input_data, config, tool_name, tool_input) if v6 and guard else None
    if _halt:
        output_json({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                            "permissionDecisionReason": _halt}})
        return

    # 提問閘：問使用者之前（settings.json PreToolUse matcher 掛了這兩個工具才會走到這裡；沒掛時由 PostToolBatch 事後提醒）
    if tool_name in ("AskUserQuestion", "ExitPlanMode"):
        if guard:
            _ask_gate(tool_name, input_data, config)
        else:
            output_nothing()
        return

    # MCP 改卡的三個工具（settings.json PreToolUse matcher 有掛）：只過上面的讀檔補帳與連鎖停手，不走寫檔閘
    if tool_name.endswith(("__atom_write", "__atom_edit_meta", "__atom_retire")):
        output_nothing()
        return

    # ─── Sub-agent 記憶注入 ────────────────────────────────────
    # sub-agent（Agent/Task）開全新 context、不觸發 UserPromptSubmit，吃不到 atom。
    # Agent／Task 派的在這裡依任務 prompt 挑卡、經 updatedInput 塞在 prompt 前面；不經 Agent 工具的子代理
    # （skill 的 context: fork、Workflow）由 SubagentStart 補（handlers/subagent_start，規則 SUBAGENT_START_NON_AGENT_INJECT）。
    # fail-open：任何錯誤都不擋 spawn。
    if tool_name in ("Agent", "Task"):
        try:
            orig_prompt = tool_input.get("prompt", "") or ""
            # fork 子代理（CC 2.1.232 起）繼承父對話與 prompt cache：父對話已注入的 atom 它看得到，
            # 只補父 context 沒有的。一般子代理開全新 context、不繼承 → already_injected 不能套。
            subagent_type = str(tool_input.get("subagent_type") or "")
            already: List[str] = []
            if subagent_type == "fork":
                sid = input_data.get("session_id", "") or ""
                state = read_state(sid) if sid else None
                already = list((state or {}).get("injected_atoms") or [])
            blob, injected = pick_subagent_memory(orig_prompt, already)
            if blob:
                new_input = dict(tool_input)
                new_input["prompt"] = "\n\n".join(p for p in (blob, orig_prompt) if p)
                _atom_debug_log(
                    "SubagentInject",
                    f"tool={tool_name} type={subagent_type or '-'} injected={injected} "
                    f"parent_already={len(already)} "
                    f"blob_tokens=~{len(blob) // 4} prompt_head={orig_prompt[:60]!r}",
                    config,
                )
                output_json({
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "updatedInput": new_input,
                    }
                })
                return
        except Exception as e:
            try:
                sys.stderr.write(f"sub-agent inject error: {e}\n")
            except OSError:
                pass
        output_nothing()
        return

    # Advisory：feedback-* 寫入舊位址提示（不擋）
    advisory = _check_feedback_routing_advisory(tool_name, tool_input)
    if advisory:
        try:
            sys.stderr.write(advisory + "\n")
        except OSError:
            pass
    _hook_lap("前置提示")

    # ─── 插件的提示（warn-only、fail-open；deny 觸發時警告只留 stderr）───
    # additionalContext 隨工具結果進下一輪（非寫前攔截）；systemMessage 同步給使用者。
    plugin_ctx = dict(input_data=input_data, config=config, session_id=input_data.get("session_id", "") or "",
                      tool_name=tool_name, tool_input=tool_input)
    plugin_warn = "\n".join(wg_plugins.emit("PreToolUse", **plugin_ctx))
    if plugin_warn:
        try:
            sys.stderr.write(plugin_warn + "\n")
        except OSError:
            pass
    _hook_lap("插件提示")

    deny_reason = _check_memory_atom_format(tool_name, tool_input)
    if deny_reason:
        _deny(input_data, config, deny_reason)
        return

    deny_reason = check_memory_path_block(tool_name, tool_input)
    if deny_reason:
        _deny(input_data, config, deny_reason)
        return

    if not guard:   # 守門家族關（插件版）：導回 atom_write 的寫入閘到此為止；跨層、版控、驗證、病形、OverviewHub、連鎖都不跑
        _emit_warnings([plugin_warn] if plugin_warn else [], plugin_warn, plugin_ctx)
        return

    # 根層敏感檔 + Bash 全域 MCP 變更：
    # 外部專案 session 寫核心層 / 全域 MCP add/remove → deny
    _cwd = input_data.get("cwd", "") or ""
    deny_reason = (
        check_cross_realm_write(tool_name, tool_input, _cwd, config)
        or check_cross_realm_bash(tool_name, tool_input, _cwd, config, str(input_data.get("session_id") or ""))
        or check_cross_realm_mcp_cmd(tool_name, tool_input, _cwd, config)
    )
    if deny_reason:
        _deny(input_data, config, deny_reason)
        return

    # state 這裡讀一次：svn 測試路徑閘與後面的口令閘、OverviewHub 共用同一份
    _co_sid = input_data.get("session_id", "") or ""
    _co_state, _co_status = read_state_status(_co_sid) if _co_sid else (None, "missing")

    deny_reason = check_svn_test_block(tool_name, tool_input, _cwd, config, _co_state, _co_sid)
    if deny_reason:
        _deny(input_data, config, deny_reason)
        return
    _hook_lap("寫入閘")

    # 索引三檔合併驅動閘（advisory-only）——必須在隱私閘之前：resolver 會 git add 索引檔，
    # 隱私檢查要看到它 stage 完的 index。之後 privacy 仍可能 deny；已 stage 的索引檔
    # 是冪等的無害合併結果（下次同指令直接放行），不需回滾。訊息同步落 stderr：
    # 後面若 deny，stdout 只能給 deny JSON，advisory 不能就此無聲消失。
    merge_warn = check_merge_driver(tool_name, tool_input, _cwd, config)
    if merge_warn:
        try:
            sys.stderr.write(merge_warn + "\n")
        except OSError:
            pass

    # svn add 中文路徑提醒（advisory-only；同樣先落 stderr，後面 deny 時不消失）
    svn_enc_warn = check_svn_encoding(tool_name, tool_input)
    if svn_enc_warn:
        try:
            sys.stderr.write(svn_enc_warn + "\n")
        except OSError:
            pass

    # git commit 口令閘（本回合使用者原話無版控口令 → deny；fail-open）
    deny_reason = check_git_commit_order(tool_name, tool_input, config, _co_state, _cwd, _co_sid)
    if deny_reason:
        _deny(input_data, config, deny_reason)
        return

    # 驗證批次閘（整組驗證要使用者口令；同一組指令同一批改動只驗一次 → deny；不歸寫碼守門管；判定在 hooks/wg_verify_batch.py，fail-open）
    if tool_name in ("Bash", "PowerShell"):
        import wg_verify_batch
        deny_reason = wg_verify_batch.check(tool_name, tool_input, config, _co_state, _cwd, _co_sid,
                                            agent_id=str(input_data.get("agent_id") or ""), call=input_data)
        if deny_reason:
            _deny(input_data, config, deny_reason)
            return

    # git commit 隱私硬閘（staged 含隱私檔 → deny；fail-open）
    deny_reason = check_git_privacy(tool_name, tool_input, _cwd, config)
    if deny_reason:
        _deny(input_data, config, deny_reason)
        return

    # commit 前病形閘（新寫進的病 → deny；舊碼原本就有的只列出，跟其他提醒一起送）；同一回合同一個 repo 只擋一次
    deny_reason, smell_warn = check_smell_gate(tool_name, tool_input, _cwd, config) if v6 else (None, None)
    if deny_reason:
        deny_reason, smell_warn = _smell_once_per_turn(input_data, _co_state, _co_sid, _cwd, deny_reason, smell_warn)
    if deny_reason:
        _deny(input_data, config, deny_reason)
        return

    # OverviewHub 寫入檢查：寫入目標命中 readonly_paths 就擋（規則 OVERVIEW_READONLY_PATHS），設定有錯照 RULE_MODE 擋或提醒
    # （規則 GATE_MODE_TABLE）。五工具：Write／Edit／NotebookEdit 取 file_path；Bash／PowerShell 取指令裡每個寫入目標，逐目標 write_check。
    # state：沒有 state 檔（SessionStart 被跳過）就 _ensure_state 建一份再用，待提醒才落得了盤；
    # 檔在但讀不到（鎖住／JSON 壞）不建 fallback 覆蓋，本次用空 state 判（仍會擋，只是不落盤）。
    hub_warn = None if v6 else _pre_v6_hub_warn(tool_name, tool_input, _co_sid, config)
    if v6 and tool_name in ("Write", "Edit", "NotebookEdit", "Bash", "PowerShell"):
        hub_deny = None
        _hub_state = _co_state
        try:
            from wg_core import _ensure_state as _hub_ensure_state, write_paths_from_tool, write_state as _ws
            from wg_overview import write_check as _hub_write_check
            _hub_targets = write_paths_from_tool(tool_name, tool_input, _cwd)
            if _hub_targets:
                _hub_state = _co_state
                if _hub_state is None and _co_sid and _co_status == "missing":
                    _hub_state = _hub_ensure_state(_co_sid, input_data, config)
                _hub_persist = _hub_state is not None and bool(_co_sid)
                if _hub_state is None:
                    _hub_state = {}
                _hub_warns: List[str] = []
                hub_changed = False
                for _hub_fp in _hub_targets:
                    _w, _d, _hit = _hub_write_check(_co_sid, _hub_fp, config)
                    if _w and not _d:              # 有 deny 理由時警告文字就是擋訊息本身，不當警告送
                        _hub_warns.append(_w)
                    if _d and (_hit is None or _rr.gate_mode(_hit["rule"], v6) == _rr.BLOCK):
                        hub_deny = _d
                        break
                    if _hit:
                        # 規則 GATE_MODE_TABLE：設定壞 → 不擋，下一個工具結果附一行
                        hub_changed = _remind_later(input_data, _hub_state, _hit, "overview_hub", path=_hub_fp) or hub_changed
                if _hub_persist and hub_changed:   # 放行也要落盤（待提醒）
                    _ws(_co_sid, _hub_state)
                    # write_state 失敗只吞 OSError 不回報：回讀磁碟核對，寫不進去就照 GATE_SELF_ERROR 報
                    _cid = (_hub_state.get("session") or {}).get("id") or _co_sid
                    _dst = read_state_status(_cid)[1]
                    if _dst != "ok":
                        _persist_msg = i18n.t("hook.pre_tool_use.hub_persist_failed", dst=_dst, cid=_cid[:8])
                        from wg_overview import _log as _hub_log
                        if _rr.gate_mode("GATE_SELF_ERROR", v6) == _rr.BLOCK:
                            hub_deny = i18n.t("hook.pre_tool_use.hub_persist_failed_block", dst=_dst, cid=_cid[:8])
                            _hub_log("state_persist_failed", _co_sid, tool=tool_name, status=_dst, deny_reason="state_unpersisted")
                        else:
                            # 規則 GATE_SELF_ERROR（remind）：state 寫不進去，待提醒也存不住 → 這一行只能跟著這次放行的警告走、stderr＋留帳
                            _hub_log("state_persist_failed", _co_sid, tool=tool_name, status=_dst, rule="GATE_SELF_ERROR", allowed=True)
                            sys.stderr.write(_persist_msg + "\n")
                            _hub_warns.append(_rr.remind_line("GATE_SELF_ERROR", i18n.t("hook.pre_tool_use.hub_persist_what", dst=_dst),
                                                              i18n.t("hook.pre_tool_use.hub_persist_fix")))
                hub_warn = "\n".join(_hub_warns) or None
        except Exception as e:
            # 閘自己炸（state 檔鎖住、import 壞、解析例外）＝判不了：送法照 RULE_MODE（GATE_SELF_ERROR：寫碼守門開提醒、關照擋）；traceback 進 stderr 與 hub log
            err = f"{type(e).__name__}: {e}"
            if _rr.gate_mode("GATE_SELF_ERROR", v6) == _rr.BLOCK:
                hub_deny = i18n.t("hook.pre_tool_use.hub_env_error_block", err=err)
                try:
                    sys.stderr.write(i18n.t("hook.pre_tool_use.hub_gate_error_denied", err=err) + "\n")
                    from wg_overview import _log as _hub_log
                    _hub_log("gate_error", _co_sid, tool=tool_name, error=err, deny_reason="gate_error")
                except Exception:
                    pass
            else:
                try:
                    sys.stderr.write(i18n.t("hook.pre_tool_use.hub_gate_error_allowed", err=err) + "\n")
                    from wg_overview import _log as _hub_log, remind_hit as _remind_hit
                    _hub_log("gate_error", _co_sid, tool=tool_name, error=err, rule="GATE_SELF_ERROR", allowed=True)
                    if _hub_state is None and _co_sid and _co_status == "missing":
                        from wg_core import _ensure_state as _hub_ensure_state2
                        _hub_state = _hub_ensure_state2(_co_sid, input_data, config)
                    if _remind_later(input_data, _hub_state, _remind_hit("GATE_SELF_ERROR", "overview_hub", i18n.t("hook.pre_tool_use.hub_gate_error_what", err=err),
                                                                        i18n.t("hook.pre_tool_use.hub_gate_error_fix")), "overview_hub"):
                        from wg_core import write_state as _ws_err
                        _ws_err(_co_sid, _hub_state)
                except Exception as e2:  # noqa: BLE001
                    sys.stderr.write(i18n.t("hook.pre_tool_use.hub_log_failed", etype=type(e2).__name__, e2=e2) + "\n")
        if hub_deny:
            _deny(input_data, config, hub_deny)
            return

    # 放行：連鎖段開著就先存要改的檔的快照（連鎖斷路器選「還原」時用）
    if v6:
        _chain_before_write(input_data, config)

    # 無 deny 才輸出警告（stdout 恆單一 JSON；不帶 permissionDecision——
    # "allow" 會自動核准繞過權限系統，advisory 不得改變放行行為）
    _emit_warnings([m for m in (plugin_warn, merge_warn, svn_enc_warn, smell_warn, hub_warn) if m], plugin_warn, plugin_ctx)


def _emit_warnings(warn_msgs: List[str], plugin_warn: str, plugin_ctx: Dict[str, Any]) -> None:
    """放行時的出口：有警告就一則 systemMessage＋additionalContext（插件的警告另通知插件點 PreToolUse.warned），沒有就不輸出。"""
    if warn_msgs:
        if plugin_warn:
            wg_plugins.emit("PreToolUse.warned", warnings=plugin_warn, **plugin_ctx)
        combined = "\n".join(warn_msgs)
        output_json({
            "systemMessage": combined,
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": combined,
            },
        })
        return

    output_nothing()
