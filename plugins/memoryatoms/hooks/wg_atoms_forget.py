"""
wg_atoms_forget.py — 選擇性遺忘：挑出可封存的卡、同步搬走並從索引刪除、觸發 sync-memory-index。

誰叫它：自我迭代（wg_atoms_iterate）與 SessionEnd。
純搬移自 wg_atoms.py，函式本體未改；hooks/wg_atoms.py 仍 re-export 這裡的名稱。
"""

import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple
from wg_core import (
    CLAUDE_DIR,
    CODE_DIR,
    MEMORY_DIR,
    _atom_debug_error,
    _atom_debug_log,
    resolve_access_json,
)
from pathlib import Path
from wg_platform import no_window_flags
import i18n   # lib/：給人看的提示走語言表
from wg_atoms_compat import ATOM_INDEX_JSON, core_protected_source, index_delete_atom, load_personal_rows


def _trigger_sync_memory_index(memory_dir: Optional[Path] = None) -> Optional[str]:
    """搬移後同步重產 MEMORY.md / _local_catalog.md / per-level _INDEX.md，回錯誤字串（None=成功）。

    set_realm / delete_atom 只改 _atom_index.json，不重產 catalog；故搬移後須補觸發（對拍
    server.js 行為）。memory_dir 給哪個記憶根就重產哪個根（專案層 `<proj>/.claude/memory`），
    不給 → 根層 memory/。同步等結果（timeout 60s）：rc≠0 / timeout / 起不來都回字串，
    呼叫者決定怎麼浮出，不靜默。
    """
    import subprocess
    cmd = [sys.executable, str(CODE_DIR / "tools" / "sync-memory-index.py"), "--write"]
    if memory_dir is not None:
        cmd += ["--memory-dir", str(memory_dir)]
    # Windows: 不帶 CREATE_NO_WINDOW 會讓子行程另開可見 console 視窗
    _no_window = no_window_flags()
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(CLAUDE_DIR), timeout=60, creationflags=_no_window, env=env,
        )
    except subprocess.TimeoutExpired:
        err = "sync-memory-index timeout (60s)"
        _atom_debug_log("ERROR", f"[realm:sync_index] {err}")
        return err
    except Exception as e:
        _atom_debug_error("realm:sync_index", e)
        return f"sync-memory-index failed to start: {type(e).__name__}: {e}"
    if proc.returncode == 0:
        return None
    tail = (proc.stderr or "").strip().splitlines()[-3:]
    err = f"sync-memory-index rc={proc.returncode}: {' | '.join(tail) or '(no stderr)'}"
    _atom_debug_log("ERROR", f"[realm:sync_index] {err}")
    return err


def select_forget_candidates(archive_candidates, config):
    """Phase D selective forgetting：從封存候選篩出可隔離者（憲法 Forgetting 對策）。

    規則：score < isolate_threshold 且 atom 名不在核心保護清單
    （lib.atom_locations.is_core_protected_name：EXACT 名單＋前綴名單，與
    distraction penalty 那側同一判定）。純函式、可測。
    """
    fcfg = ((config or {}).get("self_iteration") or {}).get("forget") or {}
    threshold = float(fcfg.get("isolate_threshold", 0.3))
    try:
        from lib.atom_locations import core_protected_source, is_core_protected_name as _is_protected
    except Exception as e:
        _atom_debug_error("forget:protected_list", e)
        return []
    if core_protected_source() != "json":   # 詞表沒載入：判不出保護名，這輪不隔離任何卡（開場紅字已講）
        return []
    out = []
    for c in (archive_candidates or []):
        if float(c.get("score", 1.0)) >= threshold:
            continue
        if _is_protected(str(c.get("atom", ""))):
            continue
        out.append(c)
    return out


def _same_file(a: Path, b: Path) -> bool:
    """同一實體檔？resolve 後 normcase 比對（Windows 大小寫不敏感），不存在的路徑也能比。"""
    try:
        return os.path.normcase(str(a.resolve(strict=False))) == os.path.normcase(str(b.resolve(strict=False)))
    except OSError:
        return False


def _forget_load_index_strict(mem_dir: Path) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """搬檔前讀 `_atom_index.json`：回 (entries, error)。

    無索引檔 → ([], None)（裸 atoms 夾，正常）；壞 JSON / 讀失敗 / 結構不對 / 任一條目不是
    {name: 非空字串, path: 非空字串} 的 dict → (None, 原因)，呼叫者本輪不搬任何檔——索引壞掉時
    搬檔會讓檔案與索引脫鉤，先停住比較安全。條目型別也嚴驗：後續刪條目按 path 比對、按 name
    交給 delete_atom，壞條目混進去會讓搬了的顆刪不到條目。
    不走 load_atom_index_json：它遇壞檔靜默回空索引，這裡要分得出「沒有」與「壞了」。
    """
    p = mem_dir / ATOM_INDEX_JSON
    if not p.exists():
        return [], None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, f"index unreadable: {type(e).__name__}: {e}"
    if not isinstance(data, dict) or not isinstance(data.get("atoms"), list):
        return None, "index unreadable: malformed structure (expected {atoms: [...]})"
    atoms = data["atoms"] + (load_personal_rows(mem_dir) if load_personal_rows else [])   # 私人卡的條目也要刪得到
    for i, a in enumerate(atoms):
        if not isinstance(a, dict):
            return None, f"index unreadable: entry #{i} malformed (expected dict, got {type(a).__name__})"
        for key in ("name", "path"):
            v = a.get(key)
            if not isinstance(v, str) or not v:
                return None, f"index unreadable: entry #{i} malformed ({key} must be non-empty str)"
    return atoms, None


def _forget_drop_index_entries(mem_dir: Path, entries: List[Dict[str, Any]],
                               moved: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """搬完後刪 `_atom_index.json` 條目：只看 path，不看名字。

    條目 path 相對 index root（mem_dir 上一層），用 _same_file 比到 src_path 才算這顆；
    命中條目用**它自己的 name＋path** 交給 delete_atom（條目名與檔名 stem 不同也刪得到），
    同名他顆的條目不碰。每筆 moved 標 `index`：removed / none（無條目）/ error（刪除失敗；
    條目留著，由下次重建索引收斂）。回 index_errors 清單。
    """
    errors: List[Dict[str, str]] = []
    todo = [m for m in moved if m.get("ok")]
    if not todo or index_delete_atom is None:
        return errors

    def _fail(m: Dict[str, Any], why: str) -> None:
        m["index"] = "error"
        m["index_error"] = why
        errors.append({"atom": m["atom"], "src_path": m["src_path"], "error": why})
        _atom_debug_log("ERROR", f"[forget:index] {m['atom']} ({m['src_path']}): {why}")

    root = mem_dir.parent
    located = [(a, a["path"]) for a in entries if isinstance(a.get("path"), str) and a.get("path")]
    for m in todo:
        src = Path(m["src_path"])
        mine = [(a.get("name"), p) for a, p in located if _same_file(root / p, src)]
        if not mine:
            m["index"] = "none"
            continue
        try:
            removed = any([index_delete_atom(mem_dir, n, path=p) for n, p in mine])
            m["index"] = "removed" if removed else "none"
        except Exception as e:
            _atom_debug_error("forget:index", e)
            _fail(m, f"delete failed: {e}")
    return errors


def apply_selective_forget(archive_candidates, config, *, atoms_dir=None,
                           staging_dir=None):
    """Phase D selective forgetting：stale+低用+非保護 atom 隔離到 `_distant/`。

    `_distant/` 已被 sync-atom-index EXCLUDED_DIR_PARTS 排除 → 搬入即不入索引/不注入、
    且可逆（搬回即復原）。流程：先嚴格讀 atoms_dir 的 `_atom_index.json`（壞掉 → 本輪
    不搬，moved 全標 error）→ 逐顆搬 MD 與 .access.json sidecar（分開記錄）→ MD 已搬走
    的顆按 path 刪索引條目（同名跨層不誤刪；sidecar 失敗只記 error 不擋索引清理）→
    同步重產該記憶根的 catalog，失敗寫 catalog_error。**預設 dry-run**（forget.enabled=false
    或 dry_run=true）→ 只寫候選清單到 _staging、不搬。憲法 selective forgetting 對策。
    回 {mode, candidates, forgotten, skipped, moved, index_errors, catalog_error}；moved 逐檔
    {atom, src_path, dst_path, ok, md_moved, sidecar_moved, error, index, index_error?}，
    ok = MD 搬成功；forgotten/skipped 是其 slug 投影。
    """
    atoms_dir = atoms_dir or MEMORY_DIR
    fcfg = ((config or {}).get("self_iteration") or {}).get("forget") or {}
    cands = select_forget_candidates(archive_candidates, config)
    if staging_dir is not None and cands:  # 候選清單寫 _staging（always；bare count → 可行動）
        try:
            staging_dir.mkdir(parents=True, exist_ok=True)
            lines = [i18n.t("hook.wg_atoms_forget.candidates_title"), ""]
            lines += [f"- {c.get('atom')} (score={c.get('score')}, "
                      f"last_used={c.get('last_used')})" for c in cands]
            (staging_dir / "forget-candidates.md").write_text(
                "\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        except OSError as e:
            _atom_debug_error("forget:write_candidates", e)
    cand_names = [c.get("atom") for c in cands]
    if not bool(fcfg.get("enabled", False)) or bool(fcfg.get("dry_run", True)):
        return {"mode": "dry_run", "candidates": cand_names, "forgotten": [], "skipped": [],
                "moved": [], "index_errors": [], "catalog_error": None}
    import shutil
    entries, index_err = _forget_load_index_strict(atoms_dir)
    moved: List[Dict[str, Any]] = []
    index_errors: List[Dict[str, str]] = []
    for c in cands:
        slug = c.get("atom")
        md = Path(c["path"]) if c.get("path") else atoms_dir / f"{slug}.md"
        # 隔離到「原範疇資料夾」下的 _distant/：restore 時直接回原範疇，不會落回 memory/ 根平鋪
        distant = md.parent / "_distant"
        item = {"atom": slug, "src_path": str(md), "dst_path": str(distant / md.name),
                "ok": False, "md_moved": False, "sidecar_moved": False, "error": ""}
        moved.append(item)
        if index_err is not None:
            item["error"] = index_err
            item["index"] = "error"
            item["index_error"] = index_err
            index_errors.append({"atom": slug, "src_path": str(md), "error": index_err})
            continue
        if not md.exists():
            item["error"] = "not found"
            continue
        try:
            distant.mkdir(parents=True, exist_ok=True)
            shutil.move(str(md), str(distant / md.name))
            item["md_moved"] = True
            item["ok"] = True
        except OSError as e:
            _atom_debug_error("forget:isolate", e)
            item["error"] = f"{type(e).__name__}: {e}"
            continue
        acc = resolve_access_json(slug, md)  # sidecar 與 md 同目錄
        if not acc.exists():
            continue
        try:
            shutil.move(str(acc), str(distant / acc.name))
            item["sidecar_moved"] = True
        except OSError as e:
            _atom_debug_error("forget:isolate_sidecar", e)
            item["error"] = f"sidecar: {type(e).__name__}: {e}"  # MD 已走，索引照刪
    if index_err is not None:
        _atom_debug_log("ERROR", f"[forget:index] {atoms_dir}: {index_err}; nothing moved")
    else:
        index_errors += _forget_drop_index_entries(atoms_dir, entries, moved)
    forgotten = [m["atom"] for m in moved if m["ok"]]
    catalog_error = None
    if forgotten:
        catalog_error = _trigger_sync_memory_index(atoms_dir)  # 重產該記憶根的 catalog（條目已刪，_distant 不入索引）
    return {"mode": "isolated", "candidates": cand_names, "forgotten": forgotten,
            "skipped": [m["atom"] for m in moved if not m["ok"]],
            "moved": moved, "index_errors": index_errors, "catalog_error": catalog_error}
