"""
handlers/post_tool_use_writes.py — 改檔帳：modified_files、未知寫入、commit

從 post_tool_use.py 拆出。做什麼：記本 session 改了哪些檔（同檔去重、子代理另記一份）；寫入目標判不出來時記「未知寫入」並從版控狀態補算；
git／svn commit 記 last_commit_turn_seq 與 committed_new_code；失敗的指令前段已寫下去的檔照記。
誰叫它：post_tool_use.handle_post_tool_use、handle_judge_post_tool_use（裁判最小記帳）、handle_post_tool_use_failure。
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import i18n   # lib/：給人看的提示走語言表
from wg_core import _now_iso, _atom_debug_error, append_guard_log, next_event_seq
from wg_lazy_re import lazy_func, lazy_module
from handlers._shared import _is_ephemeral_path, note_ephemeral_skip

_vl = lazy_module("wg_verify_ledger")
_rr = lazy_module("wg_release_rules")
_ensure_lf = lazy_func("handlers.post_tool_use_lf", "_ensure_lf")   # LF_AFTER_WRITE（守門家族；補回未知寫入只在守門開時走）
try:
    # Bash／PowerShell 改檔目標擷取（wg_core）；載不進來就只記 Edit／Write／MultiEdit／NotebookEdit 目標
    from wg_core import write_paths_from_tool as _write_paths_from_tool
except ImportError:
    _write_paths_from_tool = None


# git/svn commit 指令偵測（供 ScanReport 閘「本 turn 已 commit → 豁免收尾檢核」）。
# 限 commit 出現在首個管線/串接段之前，故 `git log | grep commit` 不誤中。
_VCS_COMMIT_RE = re.compile(r"\b(?:git|svn)\b[^|&;\n]*?\bcommit\b", re.IGNORECASE)


def _note_commit(state: Dict[str, Any], command: str, config: Optional[Dict[str, Any]] = None,
                 session_id: str = "") -> bool:
    """跑了 git／svn commit → state["last_commit_turn_seq"]＝本回合；回有沒有記。一般與裁判 PostToolUse 共用這一條：
    ScanReport 閘據此豁免收尾檢核（工作已寫進 VCS 歷史＝可稽核），寫碼查核判「改過又原樣還原」也看它（改完 commit 了，
    工作樹乾淨不代表沒改）。同時記 committed_new_code（_note_committed_new_code）。"""
    if not _VCS_COMMIT_RE.search(command):
        return False
    state["last_commit_turn_seq"] = int(state.get("turn_seq", 0))
    own = [m for m in (state.get("modified_files") or []) if (m or {}).get("session_id", session_id) == session_id]
    _note_committed_new_code(state, own, _vl.reconcile_cfg(config or {}))
    return True


def _note_committed_new_code(state: Dict[str, Any], own: List[Dict[str, Any]], cfg: Dict[str, Any]) -> None:
    """commit 之後，本 session 改過的檔所在的每個 git 根，記下 HEAD 那筆提交新增、改名、複製進來的程式檔
    （state["committed_new_code"][根]＝{shas, paths}）。讀者：寫碼查核判「內容等於已提交版本」（committed_equal_paths）認本 session 自己的提交。
    讀不到只少這份證據，stderr 由 head_new_code 說。"""
    rec = state.setdefault("committed_new_code", {})
    seen = set()
    for m in own:
        hit = _vl._git_root_of(str((m or {}).get("path") or ""))
        if not hit or hit[0] in seen:
            continue
        seen.add(hit[0])
        sha, new = _vl.head_new_code(hit[1], cfg)
        r = rec.setdefault(hit[0], {"shas": [], "paths": []})
        if sha and sha not in r["shas"]:
            r["shas"] = (r["shas"] + [sha])[-50:]
            r["paths"] = list(dict.fromkeys(r["paths"] + new))[-200:]


def _note_modified(state: Dict[str, Any], session_id: str, file_path: str, tool_name: str, agent_id: str = "",
                   via: str = "") -> None:
    """modified_files 以 (path, session_id) 去重：重複編輯只累加 count、刷 at／tool／last_turn_seq，
    不無限累積 entry。turn_seq＝第一次改的回合、last_turn_seq＝最近一次改的回合（對帳閘「本 turn 改了哪些」用）。
    消費端計「編輯次數」者（wisdom track_retry）讀 count 欄（legacy 無 count → 預設 1）。
    子代理（agent_id 非空）改的檔另記一份到 agent_modified_files[agent_id]（SubagentStop 的對帳／量尺閘只看子代理自己的）；
    主 session 的 modified_files 照舊也記，主 session 行為不變。
    sha＝寫完之後的內容雜湊（small_content_sha，太大的檔不記）；檔已不存在（rm、搬走）就留上一次的：判不出目標的寫入事後補帳時
    （UNKNOWN_WRITE_RECOVERED，_unchanged_since_noted）拿它認「記過之後沒再改」。
    via＝這一次怎麼知道它被改的：空＝工具輸入就寫明了目標；"vcs_status"＝目標判不出、事後從版控狀態補算（UNKNOWN_WRITE_RECOVERED）。"""
    try:
        sha = _vl.small_content_sha(file_path)
    except ModuleNotFoundError:   # 守門檔不出廠（插件版）：sha 只給守門家族的事後補帳用，不記
        sha = None
    _upsert_modified(state, state.setdefault("modified_files", []), session_id, file_path, tool_name, sha, via)
    if agent_id:
        _upsert_modified(state, state.setdefault("agent_modified_files", {}).setdefault(agent_id, []),
                         session_id, file_path, tool_name, sha, via)
    state["sync_pending"] = True


def _upsert_modified(state: Dict[str, Any], mods: List[Dict[str, Any]], session_id: str, file_path: str,
                     tool_name: str, sha: Optional[str] = None, via: str = "") -> None:
    """同一個檔不論哪種寫法（Edit 的反斜線、Bash 的正斜線、大小寫、`..`）只留一筆，比對用 canon_path；
    path 欄保留第一次記下的原字串。sha 是 None（檔不在、讀不到）不蓋掉舊值。via 跟著最近一次寫入（跟 tool 欄一樣）。"""
    turn = int(state.get("turn_seq", 0))
    key = _rr.canon_path(file_path)
    for m in mods:
        if (
            isinstance(m, dict)
            and m.get("path")
            and _rr.canon_path(m["path"]) == key
            and m.get("session_id", session_id) == session_id
        ):
            m["count"] = int(m.get("count", 1)) + 1
            m["at"] = _now_iso()
            m["last_at"] = m["at"]
            m["last_seq"] = next_event_seq(state)
            m["tool"] = tool_name
            m["last_turn_seq"] = turn
            m.setdefault("turn_seq", turn)
            if sha:
                m["sha"] = sha
            if via:
                m["via"] = via
            else:
                m.pop("via", None)
            break
    else:
        now = _now_iso()
        mods.append({
            "path": file_path,
            "tool": tool_name,
            "session_id": session_id,
            "at": now,
            "last_at": now,
            "last_seq": next_event_seq(state),
            "count": 1,
            "turn_seq": turn,
            "last_turn_seq": turn,
            **({"sha": sha} if sha else {}),
            **({"via": via} if via else {}),
        })


UNKNOWN_WRITES_CAP = 20
EXEC_TURN_KEYS_CAP = 50          # exec_turn_seq 最多記幾個 session／子代理（主 session＋每個子代理各一個鍵）


def _note_unknown_write(state: Dict[str, Any], session_id: str, target: str, command: str, tool_name: str,
                        agent_id: str = "", input_data: Optional[Dict[str, Any]] = None) -> List[str]:
    """寫入目標算不出來（write_paths_from_tool 回 UNKNOWN:…）→ 記一筆「未知寫入」：知道有寫、不知道寫了哪個檔，
    不能當成沒寫（規則 UNKNOWN_TARGET_LOGGED：放行並留帳）。主 session 與裁判記 unknown_writes（這個使用者回合有一筆就算寫碼回合，
    wg_core.coding_turn），子代理記 agent_unknown_writes[agent_id]；Stop 只留帳 unknown_writes_logged。每份最多留 UNKNOWN_WRITES_CAP 筆。
    有 hook 輸入（input_data）→ 規則 UNKNOWN_WRITE_RECOVERED：從版控狀態補出這條指令真的改了哪些檔（_recover_unknown_write），
    記進 modified_files，對帳、同步提醒照常判；補到的記在這一筆的 recovered，補不到的原因記 unrecovered。回補到的檔。"""
    rec: Dict[str, Any] = {"cmd": " ".join(str(command or "").split())[:120], "target": str(target)[:60], "tool": tool_name,
                           "session_id": session_id, "turn_seq": int(state.get("turn_seq", 0)), "at": _now_iso()}
    noted: List[str] = []
    if input_data is not None:
        noted, why = _recover_unknown_write(state, session_id, command, tool_name, agent_id, input_data)
        if noted:
            rec["recovered"] = noted[:10]
        if why:
            rec["unrecovered"] = why[:200]
    lst = (state.setdefault("agent_unknown_writes", {}).setdefault(agent_id, []) if agent_id
           else state.setdefault("unknown_writes", []))
    lst.append(rec)
    del lst[:-UNKNOWN_WRITES_CAP]
    return noted


UNKNOWN_SLOW_ROOTS_CAP = 10


def _recover_unknown_write(state: Dict[str, Any], session_id: str, command: str, tool_name: str, agent_id: str,
                           input_data: Dict[str, Any]) -> Tuple[List[str], str]:
    """規則 UNKNOWN_WRITE_RECOVERED（判法在 wg_unknown_write.recover）：指令開始時間從 transcript 找，這之後寫過、版控說有改動的檔
    記進 modified_files（via=vcs_status）。本 session 已經記過的檔：內容雜湊跟上次一樣、或上次記下之後 mtime 沒再變（太大的檔
    沒有雜湊，靠這一條）→ 這次沒改到它，不刷成本回合改的；暫存路徑照 Edit 的規矩濾掉。整棵查逾時的根記在
    state unknown_write_slow_roots，同 session 之後只查點名的檔。
    回 (補到的檔, 補不到或只補到一部分的原因)；耗時（含雜湊與啟動器檢查）與結果留 guard-unknown_write.jsonl。
    出錯只 stderr＋留帳、照記已補到的，不影響這次工具。"""
    import wg_unknown_write as uw
    t0 = time.monotonic()
    cwd = input_data.get("cwd", "") or (state.get("session") or {}).get("cwd", "") or ""
    try:
        started, start_from = _tool_start_bound(str(input_data.get("transcript_path") or ""),
                                                str(input_data.get("tool_use_id") or ""))
        slow = [str(r) for r in state.get("unknown_write_slow_roots") or []]
        paths, note = uw.recover(command, cwd, started, slow)
    except Exception as e:  # noqa: BLE001
        print(i18n.t("hook.post_tool_use_writes.recover_failed", error=f"{type(e).__name__}: {e}"), file=sys.stderr)
        _atom_debug_error("post_tool_use:unknown_write_recover", e)
        return [], i18n.t("hook.post_tool_use_writes.recover_failed_why", error=f"{type(e).__name__}: {e}")
    if note["slow"]:
        state["unknown_write_slow_roots"] = list(dict.fromkeys(slow + note["slow"]))[-UNKNOWN_SLOW_ROOTS_CAP:]
    before = {_rr.canon_path(m["path"]): m for m in state.get("modified_files") or []
              if isinstance(m, dict) and m.get("path") and m.get("session_id", session_id) == session_id}
    noted: List[str] = []
    why = note["why"]
    for p in paths:
        try:
            if _is_ephemeral_path(p):
                note_ephemeral_skip(state, p)      # EPHEMERAL_WRITE_NOTED_ONCE（state 跟著未知寫入一起寫回）
                continue
            if _unchanged_since_noted(p, before.get(_rr.canon_path(p))):
                continue
            _note_modified(state, session_id, p, tool_name, agent_id, via=uw.VIA)
            noted.append(p)
            _ensure_lf(p)             # LF_AFTER_WRITE：內嵌 python 寫檔（open(..., 'w')）的 CRLF 主要從這條進來
            _note_launcher(p)         # LAUNCHER_NOT_CODE：內嵌碼寫出來的啟動器也認
        except Exception as e:  # noqa: BLE001
            print(i18n.t("hook.post_tool_use_writes.note_failed", path=p, error=f"{type(e).__name__}: {e}"), file=sys.stderr)
            why = "; ".join(x for x in (why, i18n.t("hook.post_tool_use_writes.note_failed_why", name=Path(p).name, error=type(e).__name__)) if x)
    append_guard_log("unknown_write", {"session_id": session_id, "turn_seq": int(state.get("turn_seq", 0)), "tool": tool_name,
                                       "agent": bool(agent_id), "ms": int((time.monotonic() - t0) * 1000),
                                       "status_ms": note.get("ms"), "roots": note["roots"], "named": note["named"],
                                       "listed": len(paths), "recovered": len(noted), "lag_ms": note.get("lag_ms"),
                                       "start_from": start_from, "why": why})
    if why and note["roots"]:
        print(f"[Guardian:UnknownWrite] {why}", file=sys.stderr)
    return noted, why


def _unchanged_since_noted(path: str, prev: Optional[Dict[str, Any]]) -> bool:
    """本 session 已經記過這個檔，而且之後沒再改：內容雜湊跟記下的一樣；或沒有雜湊可比（太大的檔）而 mtime 不晚於上次記下的時間
    （記錄時間只到秒，放寬 1 秒）。沒記過、讀不到 → False（照補）。"""
    if not prev:
        return False
    sha = _vl.small_content_sha(path)
    if sha:
        return sha == prev.get("sha")
    from datetime import datetime
    try:
        at = datetime.fromisoformat(str(prev.get("last_at") or prev.get("at") or "")).timestamp()
        return os.path.getmtime(path) < at + 1
    except (ValueError, OSError):
        return False


def _note_launcher(path: str) -> None:
    """note_git_launcher 包一層：讀不了的內容（非 UTF-8 的 .py）、查 git 出錯只 stderr，不讓同一批後面的改檔記不到。"""
    try:
        _vl.note_git_launcher(path)
    except Exception as e:  # noqa: BLE001
        print(i18n.t("hook.post_tool_use_writes.launcher_check_failed", name=Path(path).name, error=f"{type(e).__name__}: {e}"), file=sys.stderr)


def _note_failed_writes(state: Dict[str, Any], input_data: Dict[str, Any], tool_name: str, session_id: str,
                        aid: str, cwd: str) -> Tuple[List[str], bool]:
    """失敗指令寫到的檔記進 modified_files，回 (記了哪些, state 有沒有變)。指令開始時間從 transcript 找；找不到就只要存在就記，並浮一行。
    目標判不出（heredoc 餵給直譯器、`python -c` 寫完再 exit 1）跟成功路徑一樣記未知寫入、從版控狀態補算（UNKNOWN_WRITE_RECOVERED）。"""
    started = _tool_started_at(str(input_data.get("transcript_path") or ""), str(input_data.get("tool_use_id") or ""))
    if started is None:
        print(i18n.t("hook.post_tool_use_writes.no_start_time"), file=sys.stderr)
    noted: List[str] = []
    unknown = eph = False
    seq0 = state.get("event_seq")         # 每記一筆改檔都會前進：中途出錯也看得出 state 已經改了
    command = str((input_data.get("tool_input") or {}).get("command") or "")
    try:
        for p in _write_paths_from_tool(tool_name, input_data.get("tool_input") or {}, cwd or (state.get("session") or {}).get("cwd") or "") or []:
            if p and p.startswith("UNKNOWN:"):
                unknown = True                # 先標：補算中途出錯，已經記進 state 的照樣寫回
                noted += _note_unknown_write(state, session_id, p, command, tool_name, aid, input_data)
                continue
            if p and _is_ephemeral_path(p):
                eph = note_ephemeral_skip(state, p) or eph       # EPHEMERAL_WRITE_NOTED_ONCE
                continue
            if not p or not os.path.isfile(p):
                continue
            if started is not None and os.path.getmtime(p) < started - 2:      # 指令開始前就是這樣：沒寫到（失敗在寫之前）
                continue
            _note_modified(state, session_id, p, tool_name, aid)
            noted.append(p)
    except Exception as e:  # noqa: BLE001
        _atom_debug_error("post_tool_use_failure:write_paths_from_tool", e)
    return noted, bool(noted) or unknown or eph or state.get("event_seq") != seq0


def _tool_started_at(transcript_path: str, tool_use_id: str) -> Optional[float]:
    """這次工具呼叫開始時間的下界（epoch 秒），見 _tool_start_bound；讀不到回 None。"""
    return _tool_start_bound(transcript_path, tool_use_id)[0]


def _tool_start_bound(transcript_path: str, tool_use_id: str) -> Tuple[Optional[float], str]:
    """(這次工具呼叫開始時間的下界, 怎麼得來的)：transcript 尾段（最後 512KB）找這次 tool_use 那筆 assistant 紀錄的時間（"tool_use"）。
    找不到：Claude Code 邊串流邊執行工具，同一則回覆平行送出的第一個工具跑完時，這則回覆的紀錄常常還沒寫到磁碟
    （實跑 12 次判不出目標的寫入有 3 次如此，都是同則第一個）——這則回覆一定在已落盤的最後一筆之後才開始，改用那筆的時間
    （"transcript_tail"；窗口多了模型產生這則回覆的那段）。沒有 transcript、讀不到、一筆時間都沒有 → (None, "")。"""
    from datetime import datetime
    if not transcript_path:
        return None, ""
    try:
        with open(transcript_path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 512 * 1024))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return None, ""
    last: Optional[float] = None
    for line in reversed(tail.splitlines()):
        if last is not None and (not tool_use_id or tool_use_id not in line):
            continue
        try:
            rec = json.loads(line)
            ts = datetime.fromisoformat(str(rec["timestamp"]).replace("Z", "+00:00")).timestamp() if rec.get("timestamp") else None
        except (ValueError, TypeError, KeyError):
            continue
        if ts is None:
            continue
        if tool_use_id and tool_use_id in line and rec.get("type") == "assistant":
            return ts, "tool_use"
        if last is None:
            last = ts
    return last, "transcript_tail" if last is not None else ""
