"""config_local.py — 設定怎麼疊：workflow/config.local.json（這台機器專屬、不進版控）蓋到共用設定 workflow/config.json 上
（merge_local），插件帶的出廠預設（plugin.json 的 config 欄）墊在底下（with_defaults）。

規則和 hooks/wg_core.load_config 同一套：頂層同名鍵兩邊都是物件就淺合併（上面那層的鍵優先），否則上面那層的值整個取代；
檔案不存在＝沒有本機覆寫；讀壞了 stderr 一行、照共用設定跑。
呼叫端傳 config.json 旁邊的 config.local.json（測試把 config.json 換到暫存目錄時，本機覆寫也跟著換掉）。
改過名的鍵（RENAMED_KEYS）由 apply_renamed_keys 在疊好之後對上新名：新鍵優先，只寫舊鍵照讀。
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any, Dict

try:
    from . import i18n
except ImportError:  # 頂層模組載入（含單獨按檔案載入、lib/ 不在 sys.path）：從本檔所在的 lib/ 找
    import sys as _sys
    from pathlib import Path as _Path
    if str(_Path(__file__).resolve().parent) not in _sys.path:
        _sys.path.insert(0, str(_Path(__file__).resolve().parent))
    import i18n


def _overlay(base: Dict[str, Any], top: Dict[str, Any]) -> Dict[str, Any]:
    """top 蓋到 base（原地改 base 並回傳）：頂層同名鍵兩邊都是物件就淺合併、top 的鍵優先，否則 top 的值整個取代。"""
    for key, value in top.items():
        cur = base.get(key)
        base[key] = {**cur, **value} if isinstance(cur, dict) and isinstance(value, dict) else value
    return base


def merge_local(config: Dict[str, Any], local_path: Path, tag: str = "config") -> Dict[str, Any]:
    """local_path 的內容蓋到 config（原地改並回傳）。"""
    try:
        local = json.loads(local_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return config
    except (OSError, ValueError) as e:   # ValueError 含 JSON 壞與 UTF-8 解不開
        print(i18n.t("hook.config_local.read_failed", tag=tag, path=local_path, error=e), file=sys.stderr)
        return config
    return _overlay(config, local if isinstance(local, dict) else {})


def with_defaults(config: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
    """defaults 墊在 config 底下，回新的 dict：config 沒寫的鍵補上；兩邊都是物件的段淺合併、config 的鍵優先（同 merge_local 的規則）。
    defaults 先深拷貝：呼叫端改回傳值的段，改不到預設那一份（它可能是快取）。"""
    return _overlay(copy.deepcopy(defaults), config)


# 改過名的設定鍵：(段的路徑（點分層）, 舊鍵, 新鍵)。讀取端只讀新鍵；舊鍵留在段裡不刪，還沒改讀新鍵的程式照樣讀得到。
RENAMED_KEYS = (
    ("coordination", "v6_once", "notice_once"),
    ("parallel_agents", "v6_once", "notice_once"),
    ("aidocs", "v6_once", "notice_once"),
    ("aidocs", "advisory_v6_once", "advisory_once"),
    ("self_iteration", "oscillation_v6_once", "oscillation_once"),
    ("codex_companion.soft_gate", "handoff_review_v6", "handoff_review_guarded"),
)
_renamed_hinted: set = set()   # 同一支程式同一個舊鍵只提示一次（一次 hook 會讀好幾次設定）


def apply_renamed_keys(config: Dict[str, Any]) -> Dict[str, Any]:
    """疊好的設定照 RENAMED_KEYS 對上新名（原地改並回傳）：新鍵有寫就用新鍵；只寫舊鍵 → 值抄到新鍵、stderr 一行請改名；
    兩個都寫 → 取新鍵、stderr 一行說舊鍵沒讀。補新鍵前整個頂層段換成新的一份，不改到別處共用的那一份（例：wg_core.DEFAULTS）。"""
    for path, old, new in RENAMED_KEYS:
        sec = _section(config, path)
        if sec is None or old not in sec:
            continue
        names = {"old": f"{path}.{old}", "new": f"{path}.{new}"}
        if new in sec:
            _renamed_hint(names["old"], i18n.t("hook.config_local.renamed_key_ignored", **names))
            continue
        top = path.split(".")[0]
        config[top] = copy.deepcopy(config[top])
        _section(config, path)[new] = sec[old]
        _renamed_hint(names["old"], i18n.t("hook.config_local.renamed_key", **names))
    return config


def _section(config: Dict[str, Any], path: str) -> Any:
    """設定裡 path（點分層，例 codex_companion.soft_gate）那一段；路上任一層沒寫或不是物件 → None。"""
    sec: Any = config
    for part in path.split("."):
        sec = sec.get(part) if isinstance(sec, dict) else None
    return sec if isinstance(sec, dict) else None


def _renamed_hint(old: str, text: str) -> None:
    if old in _renamed_hinted:
        return
    _renamed_hinted.add(old)
    try:
        if sys.stderr:   # pythonw 下 sys.stderr 可能是 None（print 會改寫到 stdout，弄壞 hook 輸出）
            sys.stderr.write(text + "\n")
    except (OSError, ValueError):   # stderr 寫不出去不能讓讀設定炸掉
        pass


def plugin_defaults(plugin_dir: Path) -> Dict[str, Any]:
    """插件自己帶的出廠預設：<插件資料夾>/memoryatoms-extension.json（舊名 plugin.json）的 config 欄。給不經 wg_core.load_config 的插件程式（獨立 hook、背景工具）
    自己墊在設定底下用（經 load_config 的，開著的插件已經合好）。沒有這個欄位 → {}；讀壞或不是物件 → stderr 一行、回 {}。"""
    plugin_dir = Path(plugin_dir)
    file = plugin_dir / "memoryatoms-extension.json"   # MIRROR: lib/extension_dirs.py REGISTRY／OLD_REGISTRY
    if not file.is_file():
        file = plugin_dir / "plugin.json"
    try:
        config = json.loads(file.read_text(encoding="utf-8-sig")).get("config", {})
    except (OSError, ValueError, AttributeError) as e:   # AttributeError：登記表不是物件
        config, why = None, f"{file.name}: {e}"
    else:
        why = "config is not an object"
    if isinstance(config, dict):
        return config
    if sys.stderr:   # pythonw 下 sys.stderr 可能是 None（print 會改寫到 stdout，弄壞 hook 輸出）
        sys.stderr.write(i18n.t("hook.wg_plugins.broken", name=plugin_dir.name, why=why) + "\n")
    return {}
