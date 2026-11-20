"""
wg_trigger_handling.py — 觸發詞處理表：trigger 命中的詞被表歸成「常見詞」時，改送一行標題或不當 trigger 證據，不再整張卡全文注入。

做什麼：
- 讀四層表（由遠到近：出廠 shipped → 根層 root → 組織層 org → 專案層 project），同名種類近層整條取代、enabled:false 拿掉遠層同名的；
- 合併後由近到遠找第一個條件全成立的種類決定一個詞的處理法；處理法登記在 HANDLERS（加新種類只改那裡）；
- 同一層至少 N 張卡共用的詞（min_shared_cards）在開場算一次存進 state["atom_index"]["trigger_handling"]，表或池變了才重算；
- card_outcome 回一張卡的結果：full（照舊全文）／title（只送一行標題）／none（不當 trigger 候選）。
表壞、缺出廠表、單條種類不合格都走 degraded.notify（開場紅字），壞的那層或那條等於不存在＝照舊注入；整支 fail-open。

誰叫它：handlers/ups_search.collect_matched_atoms（判結果）、handlers/ups_inject（寫碼回合補整段）、handlers/session_start（開場編譯）。
子代理注入（wg_atoms_budget.build_injection_blob）不經過這裡：常見詞在子代理那邊照舊全文帶入。
規則名 TRIGGER_HANDLING_BY_KIND；行為驗證 hooks/verify/verify_trigger_handling.py。
"""

import os
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple
import json
import re

from wg_core import CLAUDE_DIR, _atom_debug_error
import edition      # lib/（hooks 已把 lib 放進 sys.path）
import degraded     # lib/：一定用頂層模組名，跟 wg_core 共用同一個 _found
from wg_atoms_match import hit_terms

TABLE_REL = "atoms-custom/trigger-handling.json"                  # 根層／組織層／專案層用的檔名
SHIPPED_REL = "atoms-custom/trigger-handling.default.json"        # 出廠表（程式碼根；使用者不改它）
SCHEMA_VERSION = 1
LAYERS = ("global", "org", "project")
ORIGINS = ("shipped", "root", "org", "project")
FULL, TITLE, NONE = "full", "title", "none"

DATA_ROOT: Path = CLAUDE_DIR          # 資料根（測試 monkeypatch 這個）
CODE_ROOT: Path = edition.CODE_ROOT   # 程式碼根（測試 monkeypatch 這個）

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_KIND_KEYS = {"name", "enabled", "match", "handling", "params", "note"}
_MATCH_KEYS = {"terms", "script", "max_len", "min_shared_cards", "layers"}
_RANK = {NONE: 0, TITLE: 1, FULL: 2}


class Handler(NamedTuple):
    params: Dict[str, Tuple[type, Any, Any]]            # 參數名 → (型別, 預設值, 下限或 None)
    decide: Callable[[Dict[str, Any], int, bool], str]  # (params, extra, other_route) → FULL | TITLE | NONE


# 處理法登記表：加一種新處理法只在這裡加一筆（decide 只能回 FULL／TITLE／NONE）
HANDLERS: Dict[str, Handler] = {
    "title_only": Handler({}, lambda p, extra, other: FULL if other else TITLE),
    "require_more": Handler({"min_extra": (int, 1, 1)},
                            lambda p, extra, other: FULL if (other or extra >= p["min_extra"]) else TITLE),
    "drop": Handler({}, lambda p, extra, other: NONE),
}


def enabled(config: Dict[str, Any]) -> bool:
    """injection.trigger_handling.enabled；缺鍵、非 dict、值不是 bool 都算開。"""
    try:
        node = ((config or {}).get("injection") or {}).get("trigger_handling")
        if isinstance(node, dict) and isinstance(node.get("enabled"), bool):
            return node["enabled"]
    except AttributeError:
        pass
    return True


def _norm_path(p: Path) -> str:
    return os.path.normcase(os.path.realpath(str(p)))


def table_paths(atom_index: Dict[str, Any]) -> List[Tuple[str, Path, bool]]:
    """[(origin, path, required)]，由遠到近：shipped → root → org → project；路徑相同的只留最遠那筆。"""
    cand: List[Tuple[str, Path, bool]] = [
        ("shipped", Path(CODE_ROOT) / SHIPPED_REL, True),
        ("root", Path(DATA_ROOT) / TABLE_REL, False),
    ]
    org_base = (atom_index or {}).get("org_base")
    if org_base:
        cand.append(("org", Path(org_base) / TABLE_REL, False))
    project_root = (atom_index or {}).get("project_root")
    if project_root:
        cand.append(("project", Path(project_root) / ".claude" / TABLE_REL, False))
    seen: set = set()
    out: List[Tuple[str, Path, bool]] = []
    for origin, path, required in cand:
        key = _norm_path(path)
        if key in seen:
            continue
        seen.add(key)
        out.append((origin, path, required))
    return out


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _validate_kind(raw: Any, seen: set) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """回 (種類或 None, 錯誤碼或 None)。enabled:false 的條目只驗 name，回 {"name", "enabled": False}。"""
    if not isinstance(raw, dict):
        return None, "name_invalid"
    name = raw.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return None, "name_invalid"
    if name in seen:
        return None, "duplicate_invalid"
    seen.add(name)
    if "enabled" in raw and not isinstance(raw["enabled"], bool):
        return None, "enabled_invalid"
    if raw.get("enabled") is False:
        return {"name": name, "enabled": False}, None
    if set(raw) - _KIND_KEYS:
        return None, "unknown_key_invalid"
    match = raw.get("match")
    if not isinstance(match, dict) or set(match) - _MATCH_KEYS:
        return None, "match_invalid"
    terms: List[str] = []
    if "terms" in match:
        if not isinstance(match["terms"], list) or any(not isinstance(x, str) for x in match["terms"]):
            return None, "match_invalid"
        for x in match["terms"]:
            x = x.strip().lower()
            if x and x not in terms:
                terms.append(x)
    script = match.get("script", "any")
    if script not in ("any", "ascii", "non_ascii"):
        return None, "match_invalid"
    max_len = match.get("max_len")
    if max_len is not None and (not _is_int(max_len) or max_len < 1):
        return None, "match_invalid"
    min_shared = match.get("min_shared_cards")
    if min_shared is not None and (not _is_int(min_shared) or min_shared < 2):
        return None, "match_invalid"
    layers = match.get("layers")
    if layers is not None:
        if (not isinstance(layers, list) or not layers or len(set(map(str, layers))) != len(layers)
                or any(x not in LAYERS for x in layers)):
            return None, "match_invalid"
        layers = list(layers)
    else:
        layers = list(LAYERS)
    if not terms and max_len is None and min_shared is None:
        return None, "match_invalid"
    handling = raw.get("handling")
    if not isinstance(handling, str) or handling not in HANDLERS:
        return None, "handling_invalid"
    spec = HANDLERS[handling].params
    given = raw.get("params", {})
    if not isinstance(given, dict) or set(given) - set(spec):
        return None, "params_invalid"
    params: Dict[str, Any] = {}
    for key, (typ, default, low) in spec.items():
        v = given.get(key, default)
        if typ is int and not _is_int(v):
            return None, "params_invalid"
        if typ is not int and not isinstance(v, typ):
            return None, "params_invalid"
        if low is not None and v < low:
            return None, "params_invalid"
        params[key] = v
    return {"name": name, "handling": handling, "params": params, "terms": terms, "script": script,
            "max_len": max_len, "min_shared_cards": min_shared, "layers": layers}, None


def load_table(path: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    """讀一份表 → (合格的種類清單, 錯誤清單)。缺檔 → ([], [])（缺檔算不算錯由呼叫端判）。"""
    path = Path(path)
    if not path.exists():
        return [], []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return [], [f"{path}: json_parse_failed"]
    if not isinstance(data, dict):
        return [], [f"{path}: not_an_object"]
    if not _is_int(data.get("schema")) or data.get("schema") != SCHEMA_VERSION:
        return [], [f"{path}: schema_unsupported"]
    if not isinstance(data.get("kinds"), list):
        return [], [f"{path}: kinds_not_list"]
    kinds: List[Dict[str, Any]] = []
    errors: List[str] = []
    seen: set = set()
    for i, raw in enumerate(data["kinds"]):
        kind, err = _validate_kind(raw, seen)
        if err:
            label = raw.get("name") if isinstance(raw, dict) and isinstance(raw.get("name"), str) else "?"
            errors.append(f"{path}: kind[{i}] {label}: {err}")
        elif kind:
            kinds.append(kind)
    return kinds, errors


def _mtime_ns(path: Path) -> int:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return -1


def _fingerprint(atom_index: Dict[str, Any]) -> Tuple[List[List[Any]], List[int]]:
    fp = [[str(p), _mtime_ns(p)] for _o, p, _r in table_paths(atom_index)]
    pool_sig = [len(atom_index.get(layer) or []) for layer in LAYERS]
    return fp, pool_sig


def _shared_counts(atom_index: Dict[str, Any], min_n: int) -> Dict[str, Dict[str, int]]:
    out: Dict[str, Dict[str, int]] = {}
    for layer in LAYERS:
        df: Counter = Counter()
        for entry in atom_index.get(layer) or []:
            try:
                df.update({str(t).strip().lower() for t in entry[2]})
            except (IndexError, TypeError):
                continue
        out[layer] = {t: n for t, n in df.items() if n >= min_n}
    return out


def _merge_kinds(atom_index: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """讀四層表（由遠到近）合併：同名種類近層整條取代、enabled:false 拿掉遠層同名；回 (由近到遠的種類清單, 錯誤)。"""
    errors: List[str] = []
    merged: Dict[str, Dict[str, Any]] = {}
    by_origin: Dict[str, List[Dict[str, Any]]] = {o: [] for o in ORIGINS}
    for origin, path, required in table_paths(atom_index):
        if required and not Path(path).exists():
            errors.append(f"{path}: missing_default_table")
            continue
        kinds, errs = load_table(path)
        errors.extend(errs)
        for k in kinds:
            if k.get("enabled") is False:
                merged.pop(k["name"], None)
                continue
            k = {**k, "origin": origin}
            merged[k["name"]] = k
            by_origin[origin].append(k)
    final = [k for origin in reversed(ORIGINS) for k in by_origin[origin] if merged.get(k["name"]) is k]
    return final, errors


def compile_handling(atom_index: Dict[str, Any]) -> Dict[str, Any]:
    """讀四層表、合併、算同層共用數 → 可存進 state 的 dict（JSON 可序列化）。整支 fail-open。"""
    try:
        fp, pool_sig = _fingerprint(atom_index)
    except Exception:
        fp, pool_sig = [], [0, 0, 0]
    try:
        final, errors = _merge_kinds(atom_index)
        wants = [k["min_shared_cards"] for k in final if k.get("min_shared_cards")]
        shared = _shared_counts(atom_index, min(wants)) if wants else {layer: {} for layer in LAYERS}
        return {"v": 1, "fp": fp, "pool_sig": pool_sig, "kinds": final, "shared": shared, "errors": errors}
    except Exception as e:
        _atom_debug_error("trigger_handling:compile", e)
        return {"v": 1, "fp": fp, "pool_sig": pool_sig, "kinds": [], "shared": {layer: {} for layer in LAYERS},
                "errors": ["internal_error"]}


def report(errors: List[str]) -> Optional[str]:
    """errors 空 → None；否則 degraded.notify（stderr＋開場紅字）並回這一行（呼叫端可另放進當句 context）。"""
    if not errors:
        return None
    reason = "; ".join(errors)
    degraded.notify("trigger_handling", reason=reason)
    try:
        line = degraded._LINES["trigger_handling"]({"reason": reason})
    except Exception:
        line = ""
    if degraded.TAG not in line:
        line = f"{degraded.TAG} trigger-handling table unusable ({reason})"
    return line


def ensure_compiled(atom_index: Dict[str, Any], config: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """關掉 → (None, None)，不 stat 任何檔、不寫 state。指紋（各表 mtime＋三層池大小）沒變 → 用 state 裡那份；
    否則重編譯並寫回 atom_index["trigger_handling"]。這次重編譯而且有錯 → (compiled, 紅字那一行)。"""
    if not enabled(config):
        return None, None
    try:
        fp, pool_sig = _fingerprint(atom_index)
        cur = atom_index.get("trigger_handling")
        if isinstance(cur, dict) and cur.get("v") == 1 and cur.get("fp") == fp and cur.get("pool_sig") == pool_sig:
            return cur, None
        compiled = compile_handling(atom_index)
        atom_index["trigger_handling"] = compiled
        return compiled, report(compiled.get("errors") or [])
    except Exception as e:
        _atom_debug_error("trigger_handling:ensure", e)
        return None, None


def term_kind(compiled: Dict[str, Any], layer: str, term: str) -> Optional[Dict[str, Any]]:
    """合併後由近到遠找第一個條件全成立的種類（回 compiled["kinds"] 的那一條）；都不成立 → None。"""
    t = str(term).strip().lower()
    shared = (compiled.get("shared") or {}).get(layer) or {}
    for kind in compiled.get("kinds") or []:
        if layer not in (kind.get("layers") or LAYERS):
            continue
        terms = kind.get("terms") or []
        if terms and t not in terms:
            continue
        script = kind.get("script") or "any"
        if (script == "ascii" and not t.isascii()) or (script == "non_ascii" and t.isascii()):
            continue
        if kind.get("max_len") is not None and len(t) > kind["max_len"]:
            continue
        if kind.get("min_shared_cards") is not None and shared.get(t, 0) < kind["min_shared_cards"]:
            continue
        return kind
    return None


def card_outcome(compiled: Dict[str, Any], layer: str, triggers: List[str], prompt_lower: str,
                 other_route: bool = False) -> Tuple[str, Optional[Dict[str, str]]]:
    """一張 trigger 命中的卡 → (結果, info)。沒有命中詞、或有任一個一般詞 → (FULL, None)。
    其餘每個命中詞各算一個結果，整張卡取最高；info＝{"term", "kind", "handling", "origin"}，
    term 是結果最高的那幾個詞裡照卡片觸發詞順序排第一個的。"""
    hits = hit_terms(triggers, prompt_lower)
    if not hits:
        return FULL, None
    kinds = []
    for term in hits:
        k = term_kind(compiled, layer, term)
        if k is None:
            return FULL, None
        kinds.append(k)
    best, info = NONE, None
    for i, (term, k) in enumerate(zip(hits, kinds)):
        handler = HANDLERS.get(k.get("handling"))
        if handler is None:   # 登記表沒有這個名字（存在 state 的表是舊的）：照一般詞，fail-open
            return FULL, None
        extra = sum(1 for j, o in enumerate(kinds) if j != i and o.get("handling") != "drop")
        res = handler.decide(k.get("params") or {}, extra, other_route)
        if info is None or _RANK[res] > _RANK[best]:
            best = res
            info = {"term": term, "kind": k["name"], "handling": k["handling"], "origin": k.get("origin", "")}
    return best, info
