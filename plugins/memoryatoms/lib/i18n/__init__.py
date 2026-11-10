"""i18n — 給人看的提示走語言表：程式只留鍵，文字放在同資料夾的 zh-TW.json、en.json。

做什麼：t(鍵, count=3) 依目前語言取字，把 {count} 換成值。
語言怎麼決定：環境變數 WG_LANG → 插件 userConfig 的 language（lib/edition.plugin_option：CLAUDE_PLUGIN_OPTION_LANGUAGE，插件版再看 settings.json 的 pluginConfigs）→ 資料根 workflow/config.local.json（這台專屬）的 language → workflow/config.json 的 language → 預設 en。
缺鍵或缺表：回鍵名本身並寫一行 stderr（看得到鍵，不退回別的語言、不回空字串）。
表的形狀：原始碼 repo 每語言一個 <語言>.json；插件包（每檔要 < 256 KiB）拆成 <語言>.hook.json、.cli.json、.mcp.json，兩種都認。
鍵一律寫字面字串，不組字串：這樣工具才能掃出「程式用到的鍵」和表對帳。
插件的字：鍵寫成 ext.<插件資料夾>.<短名>，表放在插件自己的 <擴充資料夾>/i18n/zh-TW.json、en.json（安裝版 extensions/<名>/，
插件版是那個 memoryatoms-<名> 插件裝的位置，lib/extension_dirs.folder_of）；
核心不認得任何插件，看鍵就知道去哪張表找（語言怎麼決定、缺表缺鍵怎麼辦都和核心表一樣）。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

try:
    from ..project_root import claude_data_dir
except ImportError:  # 頂層模組載入（hooks／CLI 把 lib 加進 sys.path）
    from project_root import claude_data_dir

DEFAULT_LANGUAGE = "en"
TABLE_DIR = Path(__file__).resolve().parent
EXTENSIONS_DIR = TABLE_DIR.parents[1] / "extensions"   # 插件自帶的表：extensions/<插件資料夾>/i18n/<語言>.json
_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
SPLIT_PARTS = ("hook", "cli", "mcp")   # 插件包把核心表依鍵前綴拆成 <語言>.<部位>.json（tools/build-plugin.py）；原始碼 repo 是單檔

_tables: Dict[str, Dict[str, str]] = {}   # 表名（en.json、extensions/<插件資料夾>/i18n/en.json）→ 內容
_config_language: Optional[str] = None
_warned: Set[str] = set()


def _warn_once(message: str) -> None:
    if message in _warned:
        return
    _warned.add(message)
    try:
        if sys.stderr:  # pythonw 下 sys.stderr 可能是 None
            sys.stderr.write(f"[i18n] {message}\n")
    except (OSError, ValueError):
        pass


def _language_in(path: Path) -> Optional[str]:
    """設定檔裡的 language；沒檔、壞檔、沒寫回 None。"""
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    language = config.get("language") if isinstance(config, dict) else None
    return str(language) if language else None


def _language_from_config() -> str:
    global _config_language
    if _config_language is None:
        workflow = claude_data_dir() / "workflow"
        _config_language = _language_in(workflow / "config.local.json") or _language_in(workflow / "config.json") or DEFAULT_LANGUAGE
    return _config_language


def _language_option() -> str:
    try:
        from .. import edition as _edition
    except ImportError:  # 頂層模組載入（hooks／CLI 把 lib 加進 sys.path）
        import edition as _edition
    return _edition.plugin_option("language")


def current_language() -> str:
    return os.environ.get("WG_LANG", "").strip() or _language_option() or _language_from_config()


def _extension_folder(name: str) -> Path:
    """擴充資料夾：安裝版就在 extensions/ 底下（不多讀設定檔）；插件版問 extension_dirs（讀 installed_plugins.json）。"""
    try:
        from .. import edition as _edition, extension_dirs as _dirs
    except ImportError:  # 頂層模組載入
        import edition as _edition
        import extension_dirs as _dirs
    return _dirs.folder_of(name) if _edition.edition() == _edition.PLUGIN else EXTENSIONS_DIR / name


def _table_of(key: str, language: str) -> Tuple[str, Path]:
    """這個鍵查哪張表 → (表名, 檔)：ext.<插件資料夾>.<短名> 查那個插件自帶的表，其他鍵查核心表。"""
    parts = key.split(".")
    if len(parts) >= 3 and parts[0] == "ext":
        name = f"extensions/{parts[1]}/i18n/{language}.json"
        return name, _extension_folder(parts[1]) / "i18n" / f"{language}.json"
    return f"{language}.json", TABLE_DIR / f"{language}.json"


def _read_table(file: Path) -> Dict[str, str]:
    """核心表有單檔 <語言>.json 就讀它；沒有就合併插件包裡拆開的三檔 <語言>.hook／cli／mcp.json（缺一檔整張算讀不到）。"""
    if file.is_file() or file.parent != TABLE_DIR:
        return json.loads(file.read_text(encoding="utf-8"))
    merged: Dict[str, str] = {}
    for part in SPLIT_PARTS:
        merged.update(json.loads(file.with_name(f"{file.stem}.{part}.json").read_text(encoding="utf-8")))
    return merged


def _table(name: str, file: Path) -> Dict[str, str]:
    if name not in _tables:
        try:
            _tables[name] = _read_table(file)
        except (OSError, ValueError) as e:
            _warn_once(f"language table {name} unavailable: {e}")
            _tables[name] = {}
    return _tables[name]


def t(key: str, **values: object) -> str:
    """取目前語言的文字；{name} 換成 values 的值，values 沒給的 {name} 原樣留著（訊息裡的字面大括號不受影響）。"""
    language = current_language()
    name, file = _table_of(key, language)
    text = _table(name, file).get(key)
    if text is None:
        _warn_once(f"missing key {key} in {name}")
        return key
    if not values:
        return text
    return _PLACEHOLDER_RE.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), text)
