"""vector_venv.py — 向量套件裝在哪、裝了沒、現在是插件版還是安裝版：開場紅字、starter、setup-vector 共用這一份。

做什麼：
- plugin_edition()：分辨插件版／安裝版的單一判斷點（之後換成 lib/edition.py 的 edition() 只改這一支）。
- venv_dir()／venv_python()：向量套件的獨立 Python 夾。插件版在 ${CLAUDE_PLUGIN_DATA}/venv（不動系統 Python）；
  沒有這個變數（安裝版）落到資料根 workflow/vector-venv。安裝版平常不建它，照舊用安裝器裝進系統 Python。
- lancedb_available()：插件版只看 venv 裡有沒有 lancedb；安裝版跑這支的 Python 自己有、或 venv 裡有 → 算裝了。只看檔案，不起子行程、不連網。
怎麼跑：程式呼叫。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Optional

try:
    from .project_root import claude_data_dir
except ImportError:  # 頂層模組載入（hooks／CLI 把 lib 加進 sys.path）
    from project_root import claude_data_dir

SETUP_COMMAND = "/memoryatoms:setup-vector"


def plugin_edition() -> bool:
    """插件版＝Claude Code 以插件身分起這支程式（環境帶 CLAUDE_PLUGIN_ROOT）。W0 的 lib/edition.py 進來後改成 edition() == "plugin"。"""
    return bool(os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip())


def venv_dir(plugin_data: Optional[str] = None) -> Path:
    """向量 venv 的位置：明給的插件資料夾 → 環境 CLAUDE_PLUGIN_DATA → 資料根 workflow/vector-venv。"""
    base = (plugin_data or os.environ.get("CLAUDE_PLUGIN_DATA", "")).strip()
    return Path(base) / "venv" if base else claude_data_dir() / "workflow" / "vector-venv"


def venv_python(plugin_data: Optional[str] = None) -> Path:
    """venv 裡的直譯器路徑（不保證存在）。"""
    root = venv_dir(plugin_data)
    return root / "Scripts" / "python.exe" if sys.platform == "win32" else root / "bin" / "python"


def venv_has_lancedb(plugin_data: Optional[str] = None) -> bool:
    """venv 的 site-packages 裡有 lancedb 套件夾（Windows Lib/site-packages，其他 lib/python3.*/site-packages）。"""
    root = venv_dir(plugin_data)
    if (root / "Lib" / "site-packages" / "lancedb").is_dir():
        return True
    return any(p.is_dir() for p in root.glob("lib/python3*/site-packages/lancedb"))


def lancedb_available(plugin_data: Optional[str] = None) -> bool:
    """插件版只認 venv 裡的 lancedb：系統 Python 剛好有不算數（D3「向量點頭才裝」——沒跑 setup-vector 就不起服務）。"""
    if plugin_edition():
        return venv_has_lancedb(plugin_data)
    return importlib.util.find_spec("lancedb") is not None or venv_has_lancedb(plugin_data)


def service_python(plugin_data: Optional[str] = None) -> str:
    """起向量服務用的直譯器：venv 在而且裝好 lancedb 就用 venv 的；沒有或裝到一半照舊用跑這支的那支。"""
    candidate = venv_python(plugin_data)
    return str(candidate) if candidate.is_file() and venv_has_lancedb(plugin_data) else sys.executable
