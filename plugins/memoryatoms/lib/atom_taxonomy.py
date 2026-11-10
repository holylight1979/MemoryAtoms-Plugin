"""atom_taxonomy.py — 核心層範疇分類法的單一資料來源（memory/_meta/taxonomy.json）。

leaf 模組：不 import atom_locations / atom_io（供它們 import，無 cycle）。
JS 端 realm.js:loadTaxonomy 讀同一份 JSON（只供錯誤訊息與快速預檢；路由邏輯 py 單源）。

fail-closed：檔缺／壞 → TaxonomyUnavailable。寫入閘據此拒寫並浮開場紅字（lib/degraded）——
不藏第二份手抄清單（那會變成 drift 來源）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

try:
    from .degraded import notify as degraded_notify
    from .project_root import claude_data_dir
    from .edition import layered_config, layered_meta
except ImportError:  # 頂層模組載入（wg_core / CLI sys.path.insert）
    from degraded import notify as degraded_notify
    from project_root import claude_data_dir
    from edition import layered_config, layered_meta

CLAUDE_DIR = claude_data_dir()   # 使用者資料根（WG_CLAUDE_DIR 或 ~/.claude），和 hooks 同一支算法
TAXONOMY_PATH = layered_meta("taxonomy.json", CLAUDE_DIR / "memory")   # 插件版：資料根沒有就用插件資料夾的出廠表
CONFIG_PATH = CLAUDE_DIR / "workflow" / "config.json"


class TaxonomyUnavailable(RuntimeError):
    """taxonomy.json 缺失／損毀／缺鍵。"""


_CACHE: Dict[str, object] = {}


def load_taxonomy(path: Optional[Path] = None, *, force: bool = False) -> dict:
    """讀並驗 taxonomy.json（快取；force 重讀）。缺鍵/型別錯 → TaxonomyUnavailable；讀的是資料根那份就報開場紅字。"""
    path = TAXONOMY_PATH if path is None else path
    key = str(path)
    if not force and key in _CACHE:
        return _CACHE[key]  # type: ignore[return-value]
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        core = data["core"]
        if not isinstance(core, dict) or not core:
            raise ValueError("core section empty")
        for name, info in core.items():
            if not isinstance(info, dict):
                raise ValueError(f"core[{name}] must be object")
        failures = data.get("failures") or {}
        if not isinstance(failures, dict):
            raise ValueError("failures section must be object")
        reserved = data.get("reserved") or []
        if not isinstance(reserved, list):
            raise ValueError("reserved must be list")
        int(data.get("name_weight", 10)); int(data.get("trigger_weight", 1))
    except (OSError, ValueError, KeyError, TypeError) as e:
        if path == TAXONOMY_PATH:
            degraded_notify("taxonomy", reason=repr(e))
        raise TaxonomyUnavailable(str(e)) from e
    _CACHE[key] = data
    return data


def core_categories(path: Optional[Path] = None) -> List[str]:
    """Lv1 正名（宣告序）。"""
    path = TAXONOMY_PATH if path is None else path
    return list(load_taxonomy(path)["core"].keys())


def category_info(name: str, path: Optional[Path] = None) -> dict:
    path = TAXONOMY_PATH if path is None else path
    return dict(load_taxonomy(path)["core"].get(name) or {})


def failures_root(path: Optional[Path] = None) -> str:
    path = TAXONOMY_PATH if path is None else path
    return str((load_taxonomy(path).get("failures") or {}).get("root") or "Failures")


def failures_topics(path: Optional[Path] = None) -> List[str]:
    """Failures 家族的 Lv2 主題清單；"same-as-core" → 與核心 Lv1 同。"""
    path = TAXONOMY_PATH if path is None else path
    f = load_taxonomy(path).get("failures") or {}
    topics = f.get("topics", "same-as-core")
    if topics == "same-as-core":
        return core_categories(path)
    return [str(t) for t in topics]


def failure_type_fallback(ftype: str, path: Optional[Path] = None) -> Optional[str]:
    """extract-worker failure_type（env/assumption/silent/cognitive）→ 主題；未列 → None。"""
    path = TAXONOMY_PATH if path is None else path
    f = load_taxonomy(path).get("failures") or {}
    return (f.get("failure_type_fallback") or {}).get(ftype)


def reserved_names(path: Optional[Path] = None) -> List[str]:
    path = TAXONOMY_PATH if path is None else path
    return [str(r) for r in load_taxonomy(path).get("reserved") or []]


def weights(path: Optional[Path] = None) -> Tuple[int, int]:
    path = TAXONOMY_PATH if path is None else path
    d = load_taxonomy(path)
    return int(d.get("name_weight", 10)), int(d.get("trigger_weight", 1))


def match_lv1(raw: Optional[str], categories: Optional[Iterable[str]] = None,
              path: Optional[Path] = None) -> Optional[str]:
    """輸入（正名／slug／別名，大小寫不分）→ Lv1 正名；不合 → None。"""
    path = TAXONOMY_PATH if path is None else path
    s = (raw or "").strip()
    if not s:
        return None
    core = load_taxonomy(path)["core"]
    cats = list(categories) if categories is not None else list(core.keys())
    if s in cats:
        return s
    low = s.casefold()
    for name in cats:
        info = core.get(name) or {}
        cands = [name, str(info.get("slug") or "")] + [str(a) for a in info.get("aliases") or []]
        if any(c and c.casefold() == low for c in cands):
            return name
    return None


def category_term_pairs(layer: str = "core", path: Optional[Path] = None) -> List[Tuple[str, str]]:
    """[(term, category)]：供 lib.atom_classify.score_by_lexicon 計分（name/trigger 命中）。

    layer='failures' 時類別名同核心主題（"same-as-core"），terms 沿用核心定義。
    """
    path = TAXONOMY_PATH if path is None else path
    core = load_taxonomy(path)["core"]
    names = failures_topics(path) if layer == "failures" else list(core.keys())
    pairs: List[Tuple[str, str]] = []
    for name in names:
        info = core.get(name) or {}
        for t in info.get("terms") or []:
            t = str(t).strip().lower()
            if t:
                pairs.append((t, name))
    return pairs


def gate_enabled(config_path: Optional[Path] = None) -> bool:
    """workflow/config.json → taxonomy.gate_enabled。只有明確寫 false 才關；沒寫、config 缺或壞 → 開：
    分類判不出來的卡要被拒，不能無聲落到 memory/ 根。"""
    config_path = CONFIG_PATH if config_path is None else config_path
    try:
        cfg = layered_config(config_path)
        return bool((cfg.get("taxonomy") or {}).get("gate_enabled", True))
    except (OSError, ValueError, AttributeError):
        return True
