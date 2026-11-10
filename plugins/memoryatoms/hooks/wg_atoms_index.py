"""
wg_atoms_index.py — 索引解析與 atom 讀取：讀 _atom_index.json／_ATOM_INDEX.md／MEMORY.md 的觸發表、專案別名、atom 全文與存取紀錄快取、scope 與可見性判斷。

誰叫它：wg_atoms_pool、wg_atoms_budget 等同族檔，以及經 wg_atoms 的外部呼叫端。
純搬移自 wg_atoms.py，函式本體未改；hooks/wg_atoms.py 仍 re-export 這裡的名稱。
"""

import json
import re
from wg_core import ATOM_INDEX, MEMORY_INDEX
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path
from wg_atoms_compat import ATOM_INDEX_JSON, load_atom_index_json, to_atom_entries


# ─── Memory Index Parsing ────────────────────────────────────────────────────

TABLE_ROW_RE = re.compile(r"^\|(.+)\|$")


ALIAS_RE = re.compile(r"^>\s*Project-Aliases:\s*(.+)", re.MULTILINE)


AtomEntry = Tuple[str, str, List[str]]


def parse_memory_index(memory_dir: Path) -> List[AtomEntry]:
    """Parse atom index, return list of (name, path, triggers).
    優先 _atom_index.json，fallback _ATOM_INDEX.md → MEMORY.md。
    """
    # prefer JSON
    if load_atom_index_json is not None:
        json_path = memory_dir / ATOM_INDEX_JSON
        if json_path.exists():
            data = load_atom_index_json(memory_dir)
            entries = to_atom_entries(data)
            if entries:
                return entries

    atom_index_path = memory_dir / ATOM_INDEX
    if atom_index_path.exists():
        try:
            text = atom_index_path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            text = None
        if text:
            return _parse_trigger_table(text)

    index_path = memory_dir / MEMORY_INDEX
    if not index_path.exists():
        return []
    try:
        text = index_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return []

    if "Status: migrated-v2.21" in text:
        root_m = re.search(r"^-\s+Root:\s*(.+)$", text, re.MULTILINE)
        if root_m:
            redirect_dir = Path(root_m.group(1).strip()) / ".claude" / "memory"
            if redirect_dir.is_dir() and redirect_dir != memory_dir:
                return parse_memory_index(redirect_dir)
        return []

    return _parse_trigger_table(text)


def _parse_trigger_table(text: str) -> List[AtomEntry]:
    atoms: List[AtomEntry] = []
    in_table = False
    for line in text.splitlines():
        stripped = line.strip()
        if not in_table:
            if stripped.startswith("| Atom") or stripped.startswith("|Atom"):
                in_table = True
                continue
        else:
            # 表內容忍（2026-05 silent-failure 真因防線 direction 1）：空行 skip 不結束表
            #（寫入端意外留空行不該 silent 掉後續 atom）；重複表頭 skip（多區塊表不誤收
            # 表頭為 atom）。僅「非空且非 |」的真內容才視為表結束。
            if stripped == "":
                continue
            if stripped.startswith("| Atom") or stripped.startswith("|Atom"):
                continue
            if stripped.startswith("|---") or stripped.startswith("| ---"):
                continue
            if not stripped.startswith("|"):
                in_table = False
                continue
            cells = [c.strip() for c in stripped.split("|") if c.strip()]
            if len(cells) >= 3:
                name = cells[0]
                rel_path = cells[1]
                # lowercase + strip + 保序去重（大小寫重複會讓 count_trigger_hits 灌水）
                triggers = list(dict.fromkeys(t.strip().lower() for t in cells[2].split(",") if t.strip()))
                atoms.append((name, rel_path, triggers))
            elif cells:
                atoms.append((cells[0], "", []))
    return atoms


def parse_project_aliases(memory_dir: Path) -> List[str]:
    """Parse > Project-Aliases: line from MEMORY.md."""
    index_path = memory_dir / MEMORY_INDEX
    if not index_path.exists():
        return []
    try:
        text = index_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return []
    m = ALIAS_RE.search(text)
    if not m:
        return []
    return [a.strip().lower() for a in m.group(1).split(",") if a.strip()]


# ─── Per-turn 讀取快取原語 ───────────────────────────────────────────────────
# UPS 管線同一顆 atom 每 prompt 會被讀 3-4 次（supersedes 掃描 / related 擴散 /
# assemble / usefulness hints），access sidecar 更多（activation / rank / hot-cold /
# hints）。cache 參數皆可選：None 時自讀（各函式保持可獨測），呼叫端傳同一 dict
# 即得單次讀取共用（hook 行程 per-event 短命，無跨 prompt 失效問題）。


def read_atom_text(
    atom_path: Path, cache: Optional[Dict[str, Optional[str]]] = None,
) -> Optional[str]:
    """讀 atom 內文（utf-8-sig）；cache 提供時同 path 只實讀一次（含失敗 None 也快取）。"""
    key = str(atom_path)
    if cache is not None and key in cache:
        return cache[key]
    try:
        text = atom_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        text = None
    if cache is not None:
        cache[key] = text
    return text


def load_access_cached(
    atom_md_path: Path, cache: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """讀 atom 的 access sidecar（正規化 dict；檔缺/損毀回 defaults）。

    優先走 lib.atom_access.read_access（v3 正規化，timestamps/α/β 欄位齊）；
    lib 不可用時退直讀 raw JSON（caller 以 .get 容忍缺欄）。cache 同 read_atom_text。
    """
    key = str(atom_md_path)
    if cache is not None and key in cache:
        return cache[key]
    data: Dict[str, Any] = {}
    try:
        from lib.atom_access import read_access
        data = read_access(atom_md_path)
    except Exception:
        acc = atom_md_path.parent / f"{atom_md_path.stem}.access.json"
        try:
            raw = json.loads(acc.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
        except (OSError, json.JSONDecodeError, ValueError):
            data = {}
    if cache is not None:
        cache[key] = data
    return data


def _find_atom_path(name: str, all_atoms: List[Tuple[AtomEntry, Path]]) -> Optional[Path]:
    for (aname, rel_path, _triggers), base_dir in all_atoms:
        if aname == name:
            return (base_dir / rel_path) if rel_path else (base_dir / "memory" / f"{name}.md")
    return None


# ─── Scope 可見性（SPEC §8.1）────────────────────────────────────────────────
# 候選池在 SessionStart 只裝「本人看得到的」atom：global + 本專案 shared/failures +
# 本人 roles + 本人 personal。之後 trigger / BM25 / vector / related 全從這個池取，
# 不各自再過濾。scope 由索引 path 推導、不信 index 的 scope 欄（自動萃取曾把
# 專案層條目寫成 global）。他專案的 atom 從不進池；他專案只靠 alias 帶入 MEMORY.md。


def scope_from_rel_path(rel_path: str, layer: str = "shared") -> str:
    """索引 path → scope 標籤：personal:<user> / role:<role>；其餘回 layer（global|shared）。
    單一來源在 lib.atom_locations.scope_from_index_path（寫入端 write_index 缺省 scope、
    sync-atom-index --fix-scope-from-path 同用）；lib 不可用時本地退化實作同規則。"""
    try:
        from atom_locations import scope_from_index_path
        return scope_from_index_path(rel_path, layer)
    except ImportError:
        pass
    parts = [p for p in str(rel_path).replace("\\", "/").split("/") if p]
    dirs = parts[:-1]
    for i, seg in enumerate(dirs):
        if seg == "personal" and i + 1 < len(dirs):
            owner = dirs[i + 1]
            if owner == "auto" and i + 2 < len(dirs):
                owner = dirs[i + 2]  # personal/auto/<user>/：自動萃取候選，仍屬該使用者
            return f"personal:{owner}"
        if seg == "roles" and i + 1 < len(dirs):
            return f"role:{dirs[i + 1]}"
    return layer


def entry_visible(rel_path: str, user: Optional[str], roles: Optional[List[str]]) -> bool:
    """personal 只給本人、role 只給持有者；shared / global 對全員可見。"""
    label = scope_from_rel_path(rel_path)
    if label.startswith("personal:"):
        # "unknown" 是身份取不到時的哨兵值，不得冒領任何人的 personal
        return bool(user) and user != "unknown" and label[len("personal:"):] == user
    if label.startswith("role:"):
        return label[len("role:"):] in set(roles or ())
    return True


def filter_visible(
    entries: List[AtomEntry], user: Optional[str], roles: Optional[List[str]],
) -> List[AtomEntry]:
    return [e for e in entries if entry_visible(e[1], user, roles)]


def visible_vector_layers(
    project_slug: str, user: Optional[str], roles: Optional[List[str]],
    include_local: bool = False, extra_layers: Optional[List[str]] = None,
) -> List[str]:
    """向量服務 layer 標籤白名單，與候選池同一套可見性（indexer 標籤：global /
    extra:local-atoms / shared:<slug> / role:<slug>:<r> / personal:<slug>:<u>）。
    extra_layers：呼叫端另算好的層（組織層 shared:<org slug>），原樣附在尾端；不傳清單不變。"""
    layers = ["global"]
    if include_local:
        layers.append("extra:local-atoms")
    if user:
        layers.append(f"personal:global:{user}")  # 本人跨專案 personal（~/.claude/memory/personal/<u>/）
    if project_slug:
        layers.append(f"shared:{project_slug}")
        for r in roles or ():
            layers.append(f"role:{project_slug}:{r}")
        if user:
            layers.append(f"personal:{project_slug}:{user}")
    for layer in extra_layers or ():
        if layer not in layers:
            layers.append(layer)
    return layers
