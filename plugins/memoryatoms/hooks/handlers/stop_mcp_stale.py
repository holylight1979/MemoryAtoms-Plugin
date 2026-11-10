"""stop_mcp_stale.py — MCP 程式碼過期偵測：本 session 有沒有碰過 MCP 程式、跑著的 MCP 行程是哪些、啟動時間比程式檔舊多少（要不要提醒重載）。
由 handlers/stop.py 的 handle_stop 叫用；本檔不 import 入口檔。
"""

import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path
from wg_core import WORKFLOW_DIR
import i18n   # lib/：給人看的提示走語言表
from wg_platform import no_window_flags


_MCP_CODE_DIR = Path(__file__).resolve().parents[2] / "tools" / "workflow-guardian-mcp"


_MCP_SEGMENT = "/tools/workflow-guardian-mcp/"


def _session_touched_mcp(state: Dict[str, Any], session_id: str) -> bool:
    """本 session（主 session＋子代理）改過記憶系統工具服務的檔（路徑含 tools/workflow-guardian-mcp/，哪一份工作樹都算）。"""
    paths = [m.get("path") for m in state.get("modified_files") or []
             if isinstance(m, dict) and m.get("session_id", session_id) == session_id]
    for mods in (state.get("agent_modified_files") or {}).values():
        paths += [m.get("path") for m in mods or [] if isinstance(m, dict)]
    return any(_MCP_SEGMENT in str(p or "").replace("\\", "/").lower() for p in paths)


def _process_rows(timeout: float = 5.0) -> Optional[List[Tuple[int, int, str, str]]]:
    """全機程序表（pid, ppid, 程式名, 指令列）；列不出回 None。只在收尾文字叫使用者重載時才查（約 1 秒）。"""
    if sys.platform == "win32":
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
               "Get-CimInstance Win32_Process | ForEach-Object { \"{0}`t{1}`t{2}`t{3}\" -f "
               "$_.ProcessId,$_.ParentProcessId,$_.Name,$_.CommandLine }"]
    else:
        cmd = ["ps", "-eo", "pid=,ppid=,comm=,args="]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           creationflags=no_window_flags())
    except (OSError, subprocess.SubprocessError) as e:
        print(i18n.t("hook.stop_mcp_stale.ps_failed", error=type(e).__name__), file=sys.stderr)
        return None
    if r.returncode != 0:
        print(i18n.t("hook.stop_mcp_stale.ps_nonzero"), file=sys.stderr)
        return None
    rows: List[Tuple[int, int, str, str]] = []
    for ln in r.stdout.splitlines():
        parts = ln.split("\t", 3) if sys.platform == "win32" else ln.split(None, 3)
        if len(parts) >= 3 and parts[0].strip().isdigit() and parts[1].strip().isdigit():
            rows.append((int(parts[0]), int(parts[1]), parts[2].strip(), parts[3] if len(parts) > 3 else ""))
    return rows


def _session_mcp_pids(rows: List[Tuple[int, int, str, str]], self_pid: int) -> set:
    """這個 session 的工具服務程序：每個 session（claude 程序）各有自己的服務（實測：服務經 pythonw 外殼掛在各自的 claude.exe 底下，
    不是整個視窗共用一個）。從 self_pid 往上找最近的 claude 程序，回它底下跑 workflow-guardian-mcp/server.js 的程序；
    找不到所屬的 claude 程序就回全機的（保守：別的 session 的舊服務會讓判斷放行）。"""
    parent = {pid: ppid for pid, ppid, _, _ in rows}
    name = {pid: n.lower() for pid, _, n, _ in rows}
    mcp = {pid for pid, _, n, c in rows                       # 只認 node 程序：啟動外殼（pythonw run-hidden.py）的指令列也帶 server.js
           if n.lower().startswith("node") and "workflow-guardian-mcp" in c and "server.js" in c}

    def ancestors(pid: int) -> List[int]:
        out: List[int] = []
        while pid in parent and parent[pid] not in out and len(out) < 30:
            pid = parent[pid]
            out.append(pid)
        return out
    session = next((p for p in ancestors(self_pid) if name.get(p, "").startswith("claude")), None)
    return mcp if session is None else {m for m in mcp if session in ancestors(m)}


def _running_mcp_pids(timeout: float = 5.0) -> Optional[set]:
    """這個 session 正在跑的工具服務程序 pid（_session_mcp_pids）；列不出程序表回 None（判不出）。"""
    rows = _process_rows(timeout)
    return None if rows is None else _session_mcp_pids(rows, os.getpid())


def _mcp_load_facts(boot_dir: Optional[Path] = None, code_dir: Optional[Path] = None
                    ) -> Tuple[List[Tuple[Optional[float], float]], Optional[float]]:
    """規則 RELOAD_ADVICE_ALREADY_LOADED 的事實，以正在跑的服務程序為準（_running_mcp_pids）：有啟動紀錄的給
    （啟動時載入的程式版本毫秒, 心跳秒）；在跑卻沒有紀錄或紀錄壞了的給（None, 現在）＝判不出；有紀錄但程序已不在的不算
    （被殺掉的測試服務來不及刪檔）；列不出程序就不給任何紀錄。另回磁碟上的程式版本毫秒（server.js 與 lib/*.js 最新的修改時間，
    和服務端 codeMtime 同算法；讀不到回 None）。"""
    boot_dir = boot_dir or Path(WORKFLOW_DIR) / "mcp-boot"
    code_dir = code_dir or _MCP_CODE_DIR
    by_pid: Dict[int, Tuple[float, float]] = {}
    for f in sorted(boot_dir.glob("*.json")) if boot_dir.is_dir() else []:
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
            by_pid[int(rec["pid"])] = (float(rec["code_mtime"]), float(rec["heartbeat_at"]) / 1000)
        except (OSError, ValueError, KeyError, TypeError) as e:
            print(i18n.t("hook.stop_mcp_stale.boot_record_unreadable", name=f.name, error=type(e).__name__), file=sys.stderr)
    running = _running_mcp_pids()
    now = time.time()
    records: List[Tuple[Optional[float], float]] = [] if running is None else [
        by_pid.get(pid, (None, now)) for pid in sorted(running)]
    try:
        disk: Optional[float] = max(p.stat().st_mtime_ns for p in [code_dir / "server.js", *(code_dir / "lib").glob("*.js")]) / 1e6
    except (OSError, ValueError):
        disk = None
    return records, disk
