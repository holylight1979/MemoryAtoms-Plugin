"""atom_debug.py — atom-debug 日誌的唯一一份寫法（hooks 與萃取 worker 共用）。

呼叫端各自決定 log 目錄（hooks 走 wg_core.logs_dir()，含測試改寫；worker 走資料根/Logs），
本模組不自己找目錄。
"""

import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


def write(tag: str, content: str, log_dir: Path, config: Optional[Dict[str, Any]] = None) -> None:
    """atom_debug 旗標開才寫；tag == "ERROR" 一律寫；空內容略過；寫檔失敗吞掉（日誌不能拖垮呼叫端）。"""
    if tag != "ERROR" and not (config or {}).get("atom_debug", False):
        return
    if not content or not content.strip():
        return
    try:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"atom-debug-{datetime.now().strftime('%Y-%m-%d_%H')}.log"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, "a", encoding="utf-8", newline="\n") as f:
            f.write(f"[{ts}][{tag}] {content.strip()}\n\n")
    except Exception:
        pass


def error_message(exc: Exception) -> str:
    """網路/IO 類錯誤一行摘要；其餘附 traceback（含 NoneType 的 traceback 噪音大，也改一行）。
    必須在 except 區塊內呼叫（traceback.format_exc 讀目前例外）。"""
    if isinstance(exc, (TimeoutError, OSError, ConnectionError)):
        return f"{type(exc).__name__}: {exc}"
    msg = traceback.format_exc()
    if "NoneType" in msg:
        msg = f"{type(exc).__name__}: {exc}"
    return msg
