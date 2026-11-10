"""degraded.py — 缺詞表、缺設定、缺向量時「開場紅字＋功能明確停用」的單一出處。

做什麼：功能發現自己少了東西就呼叫 notify(功能, reason=...)：stderr 留一行、記下來；SessionStart 用 session_lines() 收進
additionalContext。每個功能一行，寫明缺什麼、哪個功能停了、怎麼補；固定字樣 [Guardian:Degraded] 寫在語言表裡，兩個語言都原樣保留。
判「少了」與停用時怎麼表現都在功能自己那裡（載入詞表的那支程式最清楚），這裡不重寫驗證、也不藏備用表。
怎麼跑：程式呼叫；行為驗證 hooks/verify/verify_degraded.py。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Set

try:
    from .project_root import claude_data_dir
except ImportError:  # 頂層模組載入（hooks／CLI 把 lib 加進 sys.path）
    from project_root import claude_data_dir

CODE_ROOT = Path(__file__).resolve().parents[1]
TAG = "[Guardian:Degraded]"

# 缺了可以從程式碼根複製一份補回的檔（功能 → 相對路徑）：訊息裡的 {path} 是資料根那份、{seed} 是程式碼根那份
SEED_FILES = {
    "config": "workflow/config.json",
    "org_memory": "workflow/config.json",
    "realm_lexicon": "memory/_meta/realm-lexicon.json",
    "taxonomy": "memory/_meta/taxonomy.json",
    "forbidden_phrases": "memory/_meta/forbidden-phrases.json",
}

# 插件版的補法：出廠詞表與設定從插件資料夾自動墊底（lib/edition.layered_meta／layered_config），不用複製；
# 會走到降級＝資料根那份壞了、或插件資料夾那份不見了。鍵一律寫字面字串，工具才掃得到。
_PLUGIN_LINES = {
    "config": lambda v: t("hook.degraded.config_plugin", **v),
    "realm_lexicon": lambda v: t("hook.degraded.realm_lexicon_plugin", **v),
    "taxonomy": lambda v: t("hook.degraded.taxonomy_plugin", **v),
    "forbidden_phrases": lambda v: t("hook.degraded.forbidden_phrases_plugin", **v),
}


def _plugin_edition() -> bool:
    try:
        from .edition import PLUGIN, edition
    except ImportError:
        from edition import PLUGIN, edition
    return edition() == PLUGIN


_found: Dict[str, str] = {}      # 功能 → 那一行；這個行程發現的
_delivered: Set[str] = set()     # 已經交給 session_lines 呼叫端的功能


def t(key: str, **values: object) -> str:
    """語言表（第一次真的降級才載）。鍵一律寫字面字串，工具才掃得到。"""
    try:
        from .i18n import t as table_text
    except ImportError:
        from i18n import t as table_text
    return table_text(key, **values)


_LINES = {
    "config": lambda v: t("hook.degraded.config", **v),
    "org_memory": lambda v: t("hook.degraded.org_memory", **v),
    "org_memory_roots": lambda v: t("hook.degraded.org_memory_roots", **v),
    "realm_lexicon": lambda v: t("hook.degraded.realm_lexicon", **v),
    "taxonomy": lambda v: t("hook.degraded.taxonomy", **v),
    "forbidden_phrases": lambda v: t("hook.degraded.forbidden_phrases", **v),
    "vector_package": lambda v: t("hook.degraded.vector_package", **v),
    "vector_invite": lambda v: t("hook.degraded.vector_invite", **v),
    "vector_embedder_invite": lambda v: t("hook.degraded.vector_embedder_invite", **v),
    "vector_embedder": lambda v: t("hook.degraded.vector_embedder", **v),
    "vector_service": lambda v: t("hook.degraded.vector_service", **v),
    "vector_foreign": lambda v: t("hook.degraded.vector_foreign", **v),
    "core_rules": lambda v: t("hook.degraded.core_rules", **v),
    "mcp": lambda v: t("hook.degraded.mcp", **v),
}


def notify(feature: str, **values: object) -> None:
    """記下「這個功能停了」並寫 stderr 一行（同一行程同一功能只寫一次）。自己在 pythonw 底下沒有 stderr 也不炸。"""
    if feature in _found:
        return
    seed = SEED_FILES.get(feature)
    if seed:
        values = {"path": claude_data_dir() / seed, "seed": CODE_ROOT / seed, **values}
    line = (_PLUGIN_LINES[feature] if seed and feature in _PLUGIN_LINES and _plugin_edition() else _LINES[feature])(values)
    if TAG not in line:   # 這個語言沒有語言表：t 回鍵名，紅字不能跟著不見
        line = f"{TAG} {line} " + " ".join(f"{k}={v}" for k, v in values.items())
    _found[feature] = line
    try:
        if sys.stderr:
            sys.stderr.write(line + "\n")
    except (OSError, ValueError):
        pass


def session_lines() -> List[str]:
    """這個行程到目前為止發現、還沒交出去的紅字，每個功能一行。"""
    new = [feature for feature in _found if feature not in _delivered]
    _delivered.update(new)
    return [_found[feature] for feature in new]
