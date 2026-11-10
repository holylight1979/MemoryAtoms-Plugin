"""extension_dirs.py — 擴充（memoryatoms-<名>）裝在哪、開著沒：hooks/wg_plugins.py（掛點）與 lib/i18n（ext.* 表）共用。

兩版各一種找法：
- 安裝版：<程式碼根>/extensions/<名>/，跟著原始碼 repo 走，就地讀。
  開關：資料夾有 .claude-plugin/plugin.json（原生 manifest）的，enabledPlugins 裡 <名>@memory-atoms 或
  memoryatoms-<名>@memory-atoms 是 true 才掛；沒有原生 manifest 的有檔就掛。
- 插件版：每個擴充是獨立插件，Claude Code 把它裝在別處。位置讀 <CLAUDE_CONFIG_DIR 或 ~/.claude>/plugins/installed_plugins.json：
      {"version": 2, "plugins": {"<插件>@<市集>": [{"scope": "user", "installPath": "...", "projectPath": "..."(專案層才有)}]}}
  只認插件名 memoryatoms-<名>；同一插件有幾筆時，projectPath 等於目前專案的優先，否則第一筆 installPath 還在的。
  開關：那個「<插件>@<市集>」在 enabledPlugins 是 true 才掛。
  找不到（enabledPlugins 開了某個 memoryatoms-*，installed_plugins.json 卻沒有它、讀不了、或 installPath 不在）→ 交給 warn 印
  [Guardian:Degraded] 一行，不無聲。
enabledPlugins：使用者層 → 專案層 → 專案本機層的 settings，後蓋前、逐鍵合併（同 Claude Code；受管設定與 --settings 不讀）。

登記表（行程內插件點那份）檔名 memoryatoms-extension.json；舊名 plugin.json 照讀，stderr 一行遷移提示（不算降級）。
兩個都在就用新名、不提示。
MIRROR: tools/workflow-guardian-mcp/lib/extension-dirs.js（找法、開關、檔名、提示要同步）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional

try:
    from . import edition as _edition
except ImportError:  # 頂層模組載入（hooks／CLI 把 lib 加進 sys.path）
    import edition as _edition

PREFIX = "memoryatoms-"                     # 擴充插件名前綴；後面那段＝資料夾短名＝ext.<名>.* 的 <名>
REGISTRY = "memoryatoms-extension.json"     # 行程內登記表（不叫 plugin.json，免得和 Claude Code 的原生 manifest 混）
OLD_REGISTRY = "plugin.json"
LOCAL_MARKETPLACE = "memory-atoms"          # 安裝版 extensions/.claude-plugin/marketplace.json 的 name；MIRROR: tools/install.py
EXTENSIONS_DIR = _edition.CODE_ROOT / "extensions"

Warn = Callable[[str, str], None]


class Extension(NamedTuple):
    name: str        # 短名（資料夾名、登記表 name、ext.<名>.*）
    folder: Path
    enabled: bool


_migrate_noted: set = set()


def registry_file(folder: Path) -> Optional[Path]:
    """這個擴充的登記表：新名優先；只有舊名時 stderr 一行遷移提示（一個行程一個擴充一次）。"""
    new = folder / REGISTRY
    if new.is_file():
        return new
    old = folder / OLD_REGISTRY
    if not old.is_file():
        return None
    if folder not in _migrate_noted:
        _migrate_noted.add(folder)
        line = f"[Guardian:Migrate] extension {folder.name}: rename {OLD_REGISTRY} to {REGISTRY} (the old name still loads)"
        try:
            from i18n import t
            line = t("hook.extension_dirs.old_registry", name=folder.name, old=OLD_REGISTRY, new=REGISTRY)
        except Exception:  # noqa: BLE001  語言表載不起來也要看得到這一行
            pass
        try:
            if sys.stderr:  # pythonw 下 sys.stderr 可能是 None
                sys.stderr.write(line + "\n")
        except (OSError, ValueError):
            pass
    return old


def _read_json(file: Path) -> Any:
    return json.loads(file.read_text(encoding="utf-8-sig"))


def enabled_plugins(warn: Warn) -> Dict[str, Any]:
    """Claude Code 的插件開關，三層後蓋前。沒有檔＝沒話說；壞檔交 warn。"""
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or ".") / ".claude"
    merged: Dict[str, Any] = {}
    for file in (_edition.claude_config_dir() / "settings.json", project / "settings.json", project / "settings.local.json"):
        try:
            merged.update(_read_json(file).get("enabledPlugins") or {})
        except OSError:
            continue
        except (ValueError, AttributeError, TypeError) as e:
            warn(str(file), f"enabledPlugins: {e}")
    return merged


def _same(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _pick_install(entries: Any) -> Optional[Path]:
    if not isinstance(entries, list):
        return None
    project = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    live = [e for e in entries if isinstance(e, dict) and e.get("installPath") and Path(str(e["installPath"])).is_dir()]
    for e in live:
        if e.get("projectPath") and _same(str(e["projectPath"]), project):
            return Path(str(e["installPath"]))
    return Path(str(live[0]["installPath"])) if live else None


def _installed(warn: Warn) -> Dict[str, Path]:
    """installed_plugins.json 裡的擴充：「插件@市集」→ 裝在哪（只收 memoryatoms-*，installPath 不在的不收）。"""
    file = _edition.claude_config_dir() / "plugins" / "installed_plugins.json"
    try:
        plugins = _read_json(file).get("plugins") or {}
    except OSError:
        return {}
    except (ValueError, AttributeError, TypeError) as e:
        warn(str(file), f"installed_plugins.json: {e}")
        return {}
    out: Dict[str, Path] = {}
    for plugin_id, entries in sorted(plugins.items()):
        if not str(plugin_id).startswith(PREFIX):
            continue
        where = _pick_install(entries)
        if where is not None:
            out[plugin_id] = where
    return out


def _from_install_edition(root: Path, warn: Warn) -> List[Extension]:
    try:
        folders = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:     # 沒有 extensions/＝純核心
        return []
    enabled = enabled_plugins(warn) if folders else {}   # 純核心不碰設定檔
    out = []
    for folder in folders:
        native = (folder / ".claude-plugin" / "plugin.json").is_file()
        on = not native or any(enabled.get(f"{n}@{LOCAL_MARKETPLACE}") is True for n in (folder.name, PREFIX + folder.name))
        out.append(Extension(folder.name, folder, on))
    return out


def _from_plugin_edition(warn: Warn) -> List[Extension]:
    enabled = enabled_plugins(warn)
    installed = _installed(warn)
    out = []
    for plugin_id, folder in installed.items():
        name = plugin_id.split("@", 1)[0][len(PREFIX):]
        out.append(Extension(name, folder, enabled.get(plugin_id) is True))
    for plugin_id, on in sorted(enabled.items()):
        if on is True and str(plugin_id).startswith(PREFIX) and plugin_id not in installed:
            warn(plugin_id.split("@", 1)[0], "enabled in settings but not found in plugins/installed_plugins.json (or its installPath is gone)")
    return sorted(out, key=lambda e: e.name)


def _ignore(_name: str, _why: str) -> None:
    pass


def extensions(warn: Warn = _ignore, root: Optional[Path] = None) -> List[Extension]:
    """所有找得到的擴充（含沒開的），照短名排序；同名只留第一個。root＝安裝版掃哪個 extensions/（預設程式碼根的）。"""
    if _edition.edition() == _edition.PLUGIN:
        found = _from_plugin_edition(warn)
    else:
        found = _from_install_edition(root or EXTENSIONS_DIR, warn)
    seen: set = set()
    out = []
    for ext in found:
        if ext.name not in seen:
            seen.add(ext.name)
            out.append(ext)
    return out


_folders: Optional[Dict[str, Path]] = None


def folder_of(name: str) -> Path:
    """擴充短名 → 資料夾（給 ext.<名>.* 找語言表；開沒開都算）。找不到回安裝版位置，讓讀表那邊照常報缺表。"""
    global _folders
    if _folders is None:
        _folders = {e.name: e.folder for e in extensions()}
    return _folders.get(name, EXTENSIONS_DIR / name)
