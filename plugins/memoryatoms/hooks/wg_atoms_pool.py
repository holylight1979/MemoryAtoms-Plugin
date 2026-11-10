"""
wg_atoms_pool.py — 候選池：關聯圖（Related 擴散）、被取代卡（Supersedes）快取、角色卡、組織層記憶目錄，最後組出本次提問可見的 atom 候選池。

誰叫它：注入管線（ups_*、subagent 注入）經 wg_atoms 呼叫。
純搬移自 wg_atoms.py，函式本體未改；hooks/wg_atoms.py 仍 re-export 這裡的名稱。
"""

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from wg_core import (
    MEMORY_DIR,
    MEMORY_INDEX,
    WORKFLOW_DIR,
    _atom_debug_error,
    _is_under_claude_dir,
    cwd_to_project_slug,
    find_project_root,
    get_project_memory_dir,
    is_cross_project_local,
)
from pathlib import Path
from atom_spec import SCOPE_DIR_NAMES
from wg_atoms_compat import ATOM_INDEX_JSON, is_local_realm_path, load_atom_index_json
from wg_atoms_index import (
    AtomEntry,
    _find_atom_path,
    filter_visible,
    parse_memory_index,
    read_atom_text,
    scope_from_rel_path,
)


# ─── Atom Matching & Activation ──────────────────────────────────────────────


# 冒號後只吃同行空白：Related 為空值時 \s* 會跨行，把下一行標題當成連結名
_RELATED_RE = re.compile(r"^- Related:[ \t]*(.+)", re.MULTILINE)


_LINK_GRAPH_CACHE: Dict[Tuple[str, ...], Tuple[Dict[str, List[str]], Dict[str, str], set]] = {}


def load_link_graph(
    memory_dirs: List[Path],
) -> Tuple[Dict[str, List[str]], Dict[str, str], set]:
    """讀索引的正向連結，在記憶體反轉成雙向鄰接表。

    回傳 (adj, alias, covered)：
      adj[name]    與它相連的名字（自己寫的正向 ＋ 別人連進來的反向），去重保序
      alias[舊名]  現名——來自 atom 的 `- Aka:` 行，改名後舊連結靠它續存
      covered      索引答得出連結的 atom 名；其餘退回讀檔解析（舊索引沒有 related 欄）

    反向連結只存在記憶體裡。落檔的話，新增一顆連到 hub 的 atom 就得去改 hub 那個檔，
    而那是別人的檔——多人共用同一個版控庫時必衝突。
    """
    memory_dirs = list(dict.fromkeys(memory_dirs))   # 呼叫端按池內每顆 atom 各給一次：去重，否則同一份索引解析幾百次
    key = tuple(sorted(str(d) for d in memory_dirs))
    hit = _LINK_GRAPH_CACHE.get(key)
    if hit is not None:
        return hit

    adj: Dict[str, List[str]] = {}
    alias: Dict[str, str] = {}
    covered: set = set()
    if load_atom_index_json is not None:
        datas = [load_atom_index_json(d) for d in memory_dirs if (d / ATOM_INDEX_JSON).exists()]
        names = {a.get("name") for d in datas for a in d.get("atoms", [])}
        for data in datas:
            for a in data.get("atoms", []):
                for old_name in a.get("aka", []) or []:
                    if old_name and old_name not in names:
                        alias.setdefault(old_name, a.get("name", ""))
        for data in datas:
            atoms = data.get("atoms", [])
            if not any("related" in a for a in atoms):
                continue  # 這份索引還沒同步過連結欄 → 整份走讀檔 fallback
            for a in atoms:
                name = a.get("name", "")
                covered.add(name)
                for target in a.get("related", []) or []:
                    target = alias.get(target, target)
                    if not target or target == name:
                        continue
                    adj.setdefault(name, []).append(target)
                    adj.setdefault(target, []).append(name)  # 反向：只在記憶體
    graph = ({k: list(dict.fromkeys(v)) for k, v in adj.items()}, alias, covered)
    _LINK_GRAPH_CACHE[key] = graph
    return graph


def spread_related(
    matched_names: set,
    all_atoms: List[Tuple[AtomEntry, Path]],
    already_injected: List[str],
    max_depth: int = 1,
    content_cache: Optional[Dict[str, Optional[str]]] = None,
) -> List[Tuple[AtomEntry, Path]]:
    """沿連結擴散，回傳尚未匹配的相關 atoms (depth-limited BFS)。

    邊以索引為準（正向 ＋ 記憶體反轉的反向）；索引沒收錄的 atom 才讀檔解析 `- Related:`。
    """
    adj, alias, covered = load_link_graph([bd / "memory" for _, bd in all_atoms])
    by_name: Dict[str, Tuple[AtomEntry, Path]] = {}
    for entry_tuple in all_atoms:
        by_name.setdefault(entry_tuple[0][0], entry_tuple)

    visited = set(matched_names) | set(already_injected)
    wave = list(matched_names)
    result: List[Tuple[AtomEntry, Path]] = []

    for _depth in range(max_depth):
        next_wave: List[str] = []
        for name in wave:
            neighbours = list(adj.get(name, ()))
            if name not in covered:
                neighbours += _related_from_file(name, all_atoms, content_cache)
            for rn in neighbours:
                rn = alias.get(rn, rn)
                if rn in visited:
                    continue
                visited.add(rn)
                entry_tuple = by_name.get(rn)
                if entry_tuple is not None:
                    result.append(entry_tuple)
                    next_wave.append(rn)
        wave = next_wave
    return result


def _related_from_file(
    name: str,
    all_atoms: List[Tuple[AtomEntry, Path]],
    content_cache: Optional[Dict[str, Optional[str]]],
) -> List[str]:
    """索引還沒收錄連結時的後路：直接讀該 atom 的 `- Related:` 行。"""
    atom_path = _find_atom_path(name, all_atoms)
    if not atom_path or not atom_path.exists():
        return []
    text = read_atom_text(atom_path, content_cache)
    if text is None:
        return []
    rm = _RELATED_RE.search(text)
    if not rm:
        return []
    return [r.strip() for r in rm.group(1).split(",") if r.strip()]


# ─── Supersedes：全路徑有效性 ────────────────────────────────────────────────
# 以前只在 UPS 對「當次命中的候選」掃 `- Supersedes:`，被取代的舊 atom 仍留在 all_atoms 池裡，
# Related 擴散、子代理注入都會把它帶回來；新 atom 沒命中時取代聲明更是讀不到。
# 改成：候選池建好就先算「被誰取代」集合（每 session 一次，stash 在 state.atom_index.superseded），
# 所有取候選的路都從去掉舊卡的池取。歷史查詢（使用者明說要看舊的）例外。

_SUPERSEDES_LINE_RE = re.compile(r"^- Supersedes:\s*(.+)", re.MULTILINE)


SUPERSEDES_MEMO = "_supersedes-memo.json"   # WORKFLOW_DIR 下，本機衍生快取：{atom 絕對路徑: [mtime_ns, size, [被取代名]]}


_SUPERSEDES_MEMO_CAP = 5000                  # 刪掉的卡不逐筆清；超過就只留這次池裡的


def _load_supersedes_memo(path: Path) -> Dict[str, list]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def collect_superseded_names(
    all_atoms: List[Tuple[AtomEntry, Path]],
    content_cache: Optional[Dict[str, str]] = None,
    memo_path: Optional[Path] = None,
) -> set:
    """掃池內每顆 atom 的 `- Supersedes:` 行，回被取代者名字集合（鏈式：A→B、B→C 都在集合）。
    只讀；讀過的內文進 content_cache 供後段續用。fail-open（讀不到的略過、下次再讀）。
    給 memo_path：卡的（mtime_ns, size）跟上次一樣就用上次的名單、不開檔，只讀新卡與改過的卡，結果寫回——
    開場逐張開幾百張卡佔 SessionStart 一半時間，機器一忙開檔最先被拖慢。"""
    memo = _load_supersedes_memo(memo_path) if memo_path else {}
    fresh: Dict[str, list] = {}
    out: set = set()
    for (name, rel_path, _t), base_dir in all_atoms:
        atom_path = (base_dir / rel_path) if rel_path else (base_dir / "memory" / f"{name}.md")
        try:
            st = atom_path.stat()
        except (OSError, ValueError):
            continue
        key, sig = str(atom_path), [st.st_mtime_ns, st.st_size]
        hit = memo.get(key)
        if isinstance(hit, list) and len(hit) == 3 and hit[:2] == sig and isinstance(hit[2], list):
            olds = [str(o) for o in hit[2]]
        else:
            text = read_atom_text(atom_path, content_cache)
            if text is None:
                continue
            m = _SUPERSEDES_LINE_RE.search(text)
            olds = [o.strip() for o in m.group(1).split(",") if o.strip() and o.strip() != name] if m else []
        fresh[key] = sig + [olds]
        out.update(olds)
    if memo_path and any(memo.get(k) != v for k, v in fresh.items()):
        merged = {**memo, **fresh}
        if len(merged) > _SUPERSEDES_MEMO_CAP:
            merged = fresh
        tmp = memo_path.with_name(f"{memo_path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8", newline="\n")
            os.replace(tmp, memo_path)
        except OSError as e:
            _atom_debug_error("supersedes_memo:write", e)
    return out


_SUPERSEDED_CACHE: Dict[str, Tuple[float, set]] = {}


def superseded_names_cached(memory_dir: Path) -> set:
    """子代理注入等沒有 session state 的路徑用：以 _atom_index.json mtime 為 key 快取一份。"""
    try:
        idx = memory_dir / ATOM_INDEX_JSON
        key = str(memory_dir)
        entries = parse_memory_index(memory_dir)
        # 快取 key 同時看索引 mtime 與 atom 檔最新 mtime：只改 atom metadata（加 Supersedes）
        # 索引不會動，單看索引 mtime 會回舊集合（Codex #8 反例）。stat 275 個檔約 5ms。
        newest_atom = 0.0
        for n, p, _t in entries:
            ap = (memory_dir.parent / p) if p else (memory_dir / f"{n}.md")
            try:
                m = ap.stat().st_mtime
                if m > newest_atom:
                    newest_atom = m
            except OSError:
                continue
        sig = (idx.stat().st_mtime if idx.exists() else 0.0, newest_atom)
        hit = _SUPERSEDED_CACHE.get(key)
        if hit and hit[0] == sig:
            return hit[1]
        pool = [((n, p, list(t)), memory_dir.parent) for n, p, t in entries]
        names = collect_superseded_names(pool, memo_path=WORKFLOW_DIR / SUPERSEDES_MEMO)
        _SUPERSEDED_CACHE[key] = (sig, names)
        return names
    except Exception:
        return set()


# ─── 候選池（SessionStart 快取與 memory_search 共用）─────────────────────────


def _collect_v4_role_atoms(
    project_mem_dir: Optional[Path], user: str, roles: List[str],
) -> List[AtomEntry]:
    """列出使用者可見的 V4 sub-layer atoms（SPEC §8.1）：shared/ 與 failures/ 全部、roles/<持有> 、personal/<本人>。"""
    if not project_mem_dir or not project_mem_dir.is_dir():
        return []
    from handlers._shared import _V4_TRIGGER_LINE_RE

    out: List[AtomEntry] = []
    mem_dir_name = project_mem_dir.name

    scan_targets: List[Path] = []
    shared = project_mem_dir / "shared"
    if shared.is_dir():
        scan_targets.append(shared)
    # failures/ 與 shared/ 同級、同樣人人可見（失敗家族 atom）。漏掃它，那些 atom 就只剩
    # 索引一條路；而 v4 layout 下索引讀來的那份會被整份丟棄 ＝ 它們永遠不會被注入。
    failures = project_mem_dir / "failures"
    if failures.is_dir():
        scan_targets.append(failures)
    roles_root = project_mem_dir / "roles"
    for r in roles:
        rd = roles_root / r
        if rd.is_dir():
            scan_targets.append(rd)
    personal_dir = project_mem_dir / "personal" / user
    if personal_dir.is_dir():
        scan_targets.append(personal_dir)

    for base in scan_targets:
        for md in sorted(base.glob("**/*.md")):
            rel_parts = md.relative_to(base).parts
            if any(p.startswith("_") for p in rel_parts[:-1]):
                continue
            if md.name in (MEMORY_INDEX, "_ATOM_INDEX.md"):
                continue
            if md.name.startswith("_") or md.name.startswith("SPEC_"):
                continue
            try:
                text = md.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError):
                continue
            tm = _V4_TRIGGER_LINE_RE.search(text)
            triggers: List[str] = []
            if tm:
                triggers = [t.strip().lower() for t in tm.group(1).split(",") if t.strip()]
            layer_rel = md.relative_to(project_mem_dir)
            rel_path = f"{mem_dir_name}/{layer_rel.as_posix()}"
            out.append((md.stem, rel_path, triggers))
    return out


def _org_memory_dir(org_root: Optional[str], project_root: Optional[Path]) -> Optional[Path]:
    """組織層記憶目錄 `<org_root>/.claude/memory`（resolve 後，slug 與 registry 一致）；
    未設定、cwd 專案根＝org 根、或目錄不存在（未 checkout）→ None。"""
    if not org_root:
        return None
    root = Path(org_root)
    try:
        root = root.resolve()
        if project_root and project_root.resolve() == root:
            return None
    except OSError:
        return None
    mem = root / ".claude" / "memory"
    return mem if mem.is_dir() else None


def build_candidate_pool(
    cwd: str, user: Optional[str], roles: Optional[List[str]], *,
    org_root: Optional[str] = None,
    global_atoms: Optional[List[AtomEntry]] = None,
) -> Dict[str, Any]:
    """本人在 cwd 看得到的 atom 候選池（純函式：不註冊專案、不建目錄、不寫檔）。

    回 {global, project, org, scopes, superseded, project_slug, project_memory_dir, project_root, org_base}。
    scope 可見性只在這裡收窄一次（personal 只給本人、role 只給持有者），下游檢索路不再各自過濾。
    global_atoms 未給就讀全域索引。
    org_root（wg_core.org_memory_root）給了就讀 `<org_root>/.claude/memory/_atom_index.json` 成 org 組
    （org_base=`<org_root>/.claude`）；cwd 的專案根就是 org 根時跳過（同一樹不重複進池）。
    """
    user = user or ""
    roles = list(roles or [])
    if global_atoms is None:
        global_atoms = parse_memory_index(MEMORY_DIR)
    # 外部專案（cwd ∉ ~/.claude）濾掉 local-realm atom，跨專案 local 例外保留；
    # is_local_realm_path 為 None（lib import 失敗）→ 不過濾（fail-open 全注入）。
    if is_local_realm_path is not None and not _is_under_claude_dir(cwd):
        global_atoms = [
            (n, p, t) for (n, p, t) in global_atoms
            if not is_local_realm_path(p) or is_cross_project_local(p)
        ]
    project_mem_dir = get_project_memory_dir(cwd)
    project_atoms = parse_memory_index(project_mem_dir) if project_mem_dir else []
    project_root = find_project_root(cwd)

    v4_entries: List[AtomEntry] = []
    try:
        v4_entries = _collect_v4_role_atoms(project_mem_dir, user, roles)
    except Exception as e:
        _atom_debug_error("candidate_pool:v4_entries", e)

    v4_layout_active = bool(project_mem_dir) and any(
        (project_mem_dir / d).is_dir() for d in SCOPE_DIR_NAMES
    )
    if v4_layout_active:
        project_merged = list(v4_entries)
    else:
        project_merged = list(project_atoms)
        existing_names = {n for n, _p, _t in project_merged}
        for name, rel_path, triggers in v4_entries:
            if name in existing_names:
                continue
            project_merged.append((name, rel_path, triggers))
            existing_names.add(name)

    org_mem_dir = _org_memory_dir(org_root, project_root)
    org_atoms = parse_memory_index(org_mem_dir) if org_mem_dir else []

    global_atoms = filter_visible(global_atoms, user, roles)
    project_merged = filter_visible(project_merged, user, roles)
    org_atoms = filter_visible(org_atoms, user, roles)
    # 同名跨層 project > org > global：後 update 者勝
    scopes = {n: scope_from_rel_path(p, "global") for n, p, _t in global_atoms}
    scopes.update({n: scope_from_rel_path(p, "org") for n, p, _t in org_atoms})
    scopes.update({n: scope_from_rel_path(p, "shared") for n, p, _t in project_merged})

    project_slug = ""
    if project_root:
        try:
            project_slug = cwd_to_project_slug(str(project_root.resolve()))
        except OSError:
            project_slug = cwd_to_project_slug(str(project_root))

    # 被取代（Supersedes）的舊卡名單；base 規則與 ups_search 一致：`_AIAtoms/` 相對專案根，其餘相對 .claude
    pool = [((n, p, t), MEMORY_DIR.parent) for n, p, t in global_atoms]
    if project_mem_dir:
        proj_parent = Path(project_mem_dir).parent
        for n, p, t in project_merged:
            base = project_root if (p.startswith("_AIAtoms/") and project_root) else proj_parent
            pool.append(((n, p, t), Path(base)))
    if org_mem_dir:
        pool.extend(((n, p, t), org_mem_dir.parent) for n, p, t in org_atoms)
    try:
        superseded = sorted(collect_superseded_names(pool, memo_path=WORKFLOW_DIR / SUPERSEDES_MEMO))
    except Exception as e:
        _atom_debug_error("candidate_pool:superseded", e)
        superseded = []

    return {
        "global": [(n, p, t) for n, p, t in global_atoms],
        "project": [(n, p, t) for n, p, t in project_merged],
        "org": [(n, p, t) for n, p, t in org_atoms],
        "scopes": scopes,
        "superseded": superseded,
        "project_slug": project_slug,
        "project_memory_dir": str(project_mem_dir) if project_mem_dir else "",
        "project_root": str(project_root) if project_root else "",
        "org_base": str(org_mem_dir.parent) if org_mem_dir else None,
    }
