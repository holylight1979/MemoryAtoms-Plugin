"""degraded.py — 缺詞表、缺設定、缺向量時「開場紅字＋功能明確停用」的單一出處。

做什麼：功能發現自己少了東西就呼叫 notify(功能, reason=...)：stderr 留一行、記下來；SessionStart 用 session_lines() 收進
additionalContext。每個功能一行，寫明缺什麼、哪個功能停了、怎麼補；固定字樣 [Guardian:Degraded] 寫在語言表裡，兩個語言都原樣保留。
判「少了」與停用時怎麼表現都在功能自己那裡（載入詞表的那支程式最清楚），這裡不重寫驗證、也不藏備用表。
怎麼跑：程式呼叫；行為驗證 hooks/verify/verify_degraded.py。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

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
_values: Dict[str, Dict[str, object]] = {}   # 功能 → notify 帶的值（解法句要用 path、seed、port 等）
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
    "org_roles": lambda v: t("hook.degraded.org_roles", **v),
    "realm_lexicon": lambda v: t("hook.degraded.realm_lexicon", **v),
    "taxonomy": lambda v: t("hook.degraded.taxonomy", **v),
    "trigger_handling": lambda v: t("hook.degraded.trigger_handling", **v),
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
    _values[feature] = dict(values)
    try:
        if sys.stderr:
            sys.stderr.write(line + "\n")
    except (OSError, ValueError):
        pass


def alert_line(state: Optional[Dict[str, Any]], source: str, target: str, level: str, scope: str, text: str,
               fix: str = "") -> str:
    """開場警告的單一出口：state=None 照舊回 text；有 state 就記進 state["phase_alerts"]（wg_phase_alerts.alert），
    open 回 text、done／report 回空字串。fix 是翻好的解法句（呼叫端寫 t("hook.phase_alerts.fix.<id>", ...)，語言表對帳工具才看得到鍵）。
    收口模組載不到時不能吞掉警告：回 text。"""
    if state is None:
        return text
    try:
        from wg_phase_alerts import alert   # hooks/（hook 行程已放進 sys.path）
    except ImportError:
        return text
    return alert(state, source, target, level, scope, text, fix)


# 功能 → (對象, level, 解法句鍵)。範圍一律是根層（資料根自己的設定、詞表、向量服務）。
# 降級紅字＝這一場的功能現在就降級（缺設定、詞表、組織層設定、向量服務），一律 open；只有邀請型是 report（使用者可用 invite=false 關）。
_ALERT_SPEC = {
    "config": ("config", "open", lambda v: t("hook.phase_alerts.fix.d01", **v)),
    "org_memory": ("org_memory", "open", lambda v: t("hook.phase_alerts.fix.d02", **v)),
    "org_memory_roots": ("org_memory_roots", "open", lambda v: t("hook.phase_alerts.fix.d03", **v)),
    "org_roles": ("org_roles", "open", lambda v: t("hook.phase_alerts.fix.d04", **v)),
    "realm_lexicon": ("realm_lexicon", "open", lambda v: t("hook.phase_alerts.fix.d05", **v)),
    "taxonomy": ("taxonomy", "open", lambda v: t("hook.phase_alerts.fix.d06", **v)),
    "forbidden_phrases": ("forbidden_phrases", "open", lambda v: t("hook.phase_alerts.fix.d07", **v)),
    "vector_package": ("vector_package", "open", lambda v: t("hook.phase_alerts.fix.d08", **v)),
    "vector_invite": ("vector_invite", "report", lambda v: t("hook.phase_alerts.fix.d09", **v)),
    "vector_embedder_invite": ("vector_embedder_invite", "report", lambda v: t("hook.phase_alerts.fix.d10", **v)),
    "vector_embedder": ("vector_embedder", "open", lambda v: t("hook.phase_alerts.fix.d11", **v)),
    "vector_service": ("vector_service", "open", lambda v: t("hook.phase_alerts.fix.d12", **v)),
    "vector_foreign": ("vector_foreign", "open", lambda v: t("hook.phase_alerts.fix.d13", **v)),
    "core_rules": ("core_rules", "open", lambda v: t("hook.phase_alerts.fix.d14", **v)),
    "mcp": ("mcp", "open", lambda v: t("hook.phase_alerts.fix.d15", **v)),
}


def session_lines(state: Optional[Dict[str, Any]] = None) -> List[str]:
    """這個行程到目前為止發現、還沒交出去的紅字，每個功能一行。
    給了 state：每個功能記成一筆結構化警告（來源 Degraded、對象＝功能名），只有 level=open 的回行文，其餘回空（不放進清單）。"""
    new = [feature for feature in _found if feature not in _delivered]
    _delivered.update(new)
    if state is None:
        return [_found[feature] for feature in new]
    out: List[str] = []
    for feature in new:
        target, level, fix_of = _ALERT_SPEC.get(feature, (feature, "open", lambda v: ""))
        line = alert_line(state, "Degraded", target, level, "root", _found[feature], fix_of(_values.get(feature, {})))
        if line:
            out.append(line)
    return out
