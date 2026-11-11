"""edition.py — 分辨這份程式是「插件版」還是「安裝版」，並提供兩版共用的分層讀設定、讀詞表。

做什麼：
- edition()：插件版回 "plugin"、安裝版回 "install"。
  判法：環境變數 CLAUDE_PLUGIN_ROOT（Claude Code 給插件 hook／MCP 的插件資料夾）等於本程式碼根 → 插件版；
  否則程式碼根有 .claude-plugin/plugin.json（插件包才有，原始碼 repo 沒有）→ 插件版（AI 從 Bash 跑的工具拿不到任何插件環境變數）；
  都不是 → 安裝版。
- plugin_option(鍵)：插件 userConfig 的值。環境變數 CLAUDE_PLUGIN_OPTION_<鍵大寫> → 插件版才看 Claude Code 使用者設定檔
  settings.json 的 pluginConfigs["<插件>@<市集>"].options.<鍵>；都沒有回 ""。一律去頭尾空白。
- layered_config()：插件版 <程式碼根>/workflow/config.json（出廠）→ 插件版 <程式碼根>/workflow/config.plugin.json →
  <資料根>/workflow/config.json → <資料根>/workflow/config.local.json，後蓋前；疊法同 lib/config_local（頂層同名段淺合併）。
  出廠兩層只在插件版墊底：安裝版程式碼根＝資料根，出廠那份就是資料根那份；資料根另指（測試、乾淨環境檢查）時照舊只讀資料根，
  缺了開場紅字（綱領「缺設定：開場紅字＋功能停用」）。同一個檔只讀一次。
- generated_language()：寫進產生檔（MEMORY.md、各層 _INDEX.md、_ATOM_INDEX.md 等）的固定文字用哪種語言，讀 repo 追蹤的設定 `generated_files.language`（"en"／"zh-TW"，缺或不認得＝"en"）。
- layered_meta(名)：詞表檔路徑；<資料根>/memory/_meta/<名> 在就用它，插件版才退到 <程式碼根> 的（理由同上）。

本模組不 import project_root（project_root.claude_data_dir 會呼叫這裡，頂層互相 import 會成環）；要資料根的函式用到才載。
MIRROR: tools/workflow-guardian-mcp/lib/paths.js（edition、pluginOption、layeredConfig、layeredMeta 同一套算法）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PLUGIN = "plugin"
INSTALL = "install"
PLUGIN_NAME = "memoryatoms"
# pluginConfigs 的鍵是「插件名@市集名」（本機實例：~/.claude/plugins/marketplaces/claude-code-plugins/mods/sec-default/
# hooks/policy/own-option/own-options-path/own-options-path.ts 的 ['pluginConfigs', '<插件>@builtin', 'options']）。
# 先找自己市集的，沒有再找任何「memoryatoms@*」（從目錄同步裝的是別的市集名）。
PLUGIN_IDS = ("memoryatoms@memoryatoms",)

CODE_ROOT = Path(__file__).resolve().parents[1]


def _same_path(a: str, b: Path) -> bool:
    """兩個路徑是不是同一處：展開 8.3 短名與連結再比，Windows 不分大小寫。"""
    def norm(p: str) -> str:
        try:
            p = os.path.realpath(p)
        except (OSError, ValueError):
            p = os.path.abspath(p)
        return os.path.normcase(os.path.normpath(p))
    return norm(a) == norm(str(b))


_edition_cache: Dict[str, str] = {}   # CLAUDE_PLUGIN_ROOT 的值 → 版別（i18n 每次取字都會問，realpath 不每次重算）


def edition() -> str:
    root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if root not in _edition_cache:
        if root and _same_path(root, CODE_ROOT):
            _edition_cache[root] = PLUGIN
        elif (CODE_ROOT / ".claude-plugin" / "plugin.json").is_file():
            _edition_cache[root] = PLUGIN
        else:
            _edition_cache[root] = INSTALL
    return _edition_cache[root]


def claude_config_dir() -> Path:
    """Claude Code 自己的設定目錄（同 project_root.claude_config_dir，這裡另寫一份免得成環）。"""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


_settings_cache: Dict[str, Any] = {}   # settings.json 路徑 → 內容（一個行程讀一次；hook 與 CLI 都是短行程）


def _settings_option(key: str) -> str:
    """settings.json 的 pluginConfigs 裡本插件的 options.<鍵>；沒檔、壞檔、沒寫都回 ""。
    只讀使用者層 settings.json；官方說 pluginConfigs 也從 --settings 與受管設定讀，那兩處這裡不讀（TODO：要支援再加）。"""
    path = str(claude_config_dir() / "settings.json")
    if path not in _settings_cache:
        try:
            _settings_cache[path] = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _settings_cache[path] = {}
    data = _settings_cache[path]
    configs = data.get("pluginConfigs") if isinstance(data, dict) else None
    if not isinstance(configs, dict):
        return ""
    ids = [i for i in PLUGIN_IDS if i in configs] + sorted(
        i for i in configs if i.split("@", 1)[0] == PLUGIN_NAME and i not in PLUGIN_IDS)
    for plugin_id in ids:
        entry = configs.get(plugin_id)
        options = entry.get("options") if isinstance(entry, dict) else None
        value = options.get(key) if isinstance(options, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def plugin_option(key: str) -> str:
    value = os.environ.get("CLAUDE_PLUGIN_OPTION_" + key.upper(), "").strip()
    if value:
        return value
    return _settings_option(key) if edition() == PLUGIN else ""


def _data_dir() -> Path:
    try:
        from .project_root import claude_data_dir
    except ImportError:  # 頂層模組載入（hooks／CLI 把 lib 加進 sys.path）
        from project_root import claude_data_dir
    return claude_data_dir()


def config_layers(config_path: Optional[Path] = None, with_local: bool = True) -> List[Path]:
    """layered_config 依序讀的檔（不管在不在）；同一個檔只列一次。
    config_path：資料根 config.json 那層換成這個檔（呼叫端模組的 CONFIG_PATH，測試會換掉它），本機覆寫取它旁邊的 config.local.json；
    with_local=False：不列本機覆寫（呼叫端自己用 config_local.merge_local 疊，例：wg_core.load_config 要先墊插件預設）。"""
    own = Path(config_path) if config_path is not None else _data_dir() / "workflow" / "config.json"
    files: List[Path] = []
    if edition() == PLUGIN:
        files += [CODE_ROOT / "workflow" / "config.json", CODE_ROOT / "workflow" / "config.plugin.json"]
    files.append(own)
    if with_local:
        files.append(own.with_name("config.local.json"))
    out: List[Path] = []
    for f in files:
        if not any(_same_path(str(f), seen) for seen in out):
            out.append(f)
    return out


def _overlay(base: Dict[str, Any], top: Dict[str, Any]) -> Dict[str, Any]:
    """同 lib/config_local._overlay：頂層同名鍵兩邊都是物件就淺合併、top 的鍵優先，否則整個取代。"""
    for key, value in top.items():
        cur = base.get(key)
        base[key] = {**cur, **value} if isinstance(cur, dict) and isinstance(value, dict) else value
    return base


def layered_config(config_path: Optional[Path] = None, with_local: bool = True,
                   failed: Optional[List[Path]] = None) -> Dict[str, Any]:
    """各層疊好的設定（參數同 config_layers）。檔不在＝這層沒有；讀壞了跳過這層、不無聲：
    failed 是 list 就把壞檔路徑放進去、由呼叫端自己回報（例：wg_core 立旗標給開場紅字）；沒給就 stderr 一行。"""
    config: Dict[str, Any] = {}
    for f in config_layers(config_path, with_local):
        try:
            layer = json.loads(f.read_text(encoding="utf-8-sig"))   # 有些編輯器存 BOM；原本幾個讀取端就容忍
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as e:
            if failed is not None:
                failed.append(f)
                continue
            try:
                try:
                    from . import i18n
                except ImportError:
                    import i18n
                print(i18n.t("hook.config_local.read_failed", tag="edition", path=f, error=e), file=sys.stderr)
            except Exception:  # noqa: BLE001  stderr 不在（pythonw）也不能讓讀設定炸掉
                pass
            continue
        if isinstance(layer, dict):
            _overlay(config, layer)
    return config


GENERATED_LANGUAGES = ("en", "zh-TW")


def generated_language(config_path: Optional[Path] = None) -> str:
    """產生檔固定文字的語言：設定 `generated_files.language`，缺鍵、壞值都回 "en"。
    刻意不看 WG_LANG、插件 userConfig 與 config.local.json（這台專屬）：產生檔進版控、多台機器共用，
    語言要由 repo 自己追蹤的設定決定，所有機器一致，才不會各台重產時把整份來回改寫成不同語言。"""
    section = layered_config(config_path, with_local=False).get("generated_files")
    value = section.get("language") if isinstance(section, dict) else None
    return value if value in GENERATED_LANGUAGES else "en"


def layered_meta(name: str, memory_dir: Optional[Path] = None) -> Path:
    """詞表檔路徑。memory_dir：呼叫端手上的記憶樹（專案層、測試的暫存樹）；不是資料根那棵就照舊用它自己的 _meta，不墊出廠表。"""
    data_memory = _data_dir() / "memory"
    if memory_dir is not None and not _same_path(str(memory_dir), data_memory):
        return Path(memory_dir) / "_meta" / name
    own = data_memory / "_meta" / name
    return own if own.is_file() or edition() != PLUGIN else CODE_ROOT / "memory" / "_meta" / name


def describe() -> Dict[str, Optional[str]]:
    """給對帳腳本與除錯用：版別、資料根、語言（字串）。"""
    try:
        from . import i18n
    except ImportError:
        import i18n
    return {"edition": edition(), "data_dir": str(_data_dir()), "language": i18n.current_language()}
