"""shipped_cards.py — 出廠卡：發版時打包、插件版第一場展開到資料根、之後只換使用者沒改過的。

做什麼：
- iter_shipped_sources(code_root)：發版要出廠的卡（memory/<範疇>/**/*.md，略過 _meta／_distant／personal／templates 等非卡片夾、
  `_` 開頭檔與 MEMORY.md）；`_reference/` 參考文也出廠（不是卡，不進索引）。tools/pack-shipped-cards.py 用它打包。
- sync_shipped(code_root, data_root)：讀 <程式碼根>/memory/shipped-manifest.json 與各包，比對 <資料根>/memory/_shipped.json
  （每張出廠卡上次展開時的指紋 fp、所根據的出廠版 sha、版本）：
    沒記錄、磁碟也沒有 → 寫入（fresh）；沒記錄、磁碟已有一樣的 → 收編（adopted）；沒記錄、磁碟已有不一樣的 → 留著（kept_user）
    有記錄、出廠版沒變 → 不動；出廠版變了、本機指紋＝記錄 → 換新版（updated，保留本機的系統欄位）；本機改過 → 留著（kept_user）
    有記錄、磁碟沒了（使用者刪、退役進 _distant/、搬去別處）→ 不復活（archived_skip），記錄標 archived
    新版拿掉：本機沒改過 → 刪檔連同 .access.json（removed）；改過 → 留著、從記錄除名（kept_user）
  manifest 的 sha 跟上次一樣就整段略過（每場開場的常態，只讀兩個小檔）。
  程式碼根＝資料根（安裝版）一律不做事。
- 「改過沒」看指紋：整張卡去掉系統會自動改的地方再算 sha256——檔頭的 Confidence／Last-used／Confirmations／ReadHits／
  Source／Quote 行、`## 演化日誌` 段、知識行開頭的 [臨]/[觀]/[固] 標記（SessionEnd 自動晉升、atom_promote 會改）、行尾空白、CRLF、BOM。
  其餘（標題、Trigger、Related、知識段、行動段…）任何一字不同都算使用者改過。
誰叫它：hooks/handlers/session_start_index.py（插件版開場，重建索引前）；tools/pack-shipped-cards.py（iter_shipped_sources）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Dict, Iterator, List, Tuple

try:
    from .atom_spec import ATOM_INDEX, MEMORY_INDEX, REFERENCE_DIR_NAME, SKIP_DIRS
except ImportError:
    from atom_spec import ATOM_INDEX, MEMORY_INDEX, REFERENCE_DIR_NAME, SKIP_DIRS

MANIFEST_NAME = "shipped-manifest.json"     # <程式碼根>/memory/ 底下，發版產物
PACK_PATTERN = "shipped-{n}.jsonl"           # 每行 {"path","sha256","text"}
STATE_NAME = "_shipped.json"                 # <資料根>/memory/ 底下，本機展開紀錄
LOCK_NAME = "_shipped.lock"
LOCK_STALE_SECS = 300                        # 鎖檔比這舊就當上一場當掉留下的

# 系統自己會改的檔頭欄位（U9：atom_promote／SessionEnd 晉升改 Confidence；wg_provenance 補 Source／Quote；
# Last-used／Confirmations／ReadHits 是舊卡殘留，現在寫在 .access.json）
SYSTEM_HEADER_KEYS = ("Confidence", "Last-used", "Confirmations", "ReadHits", "Source", "Quote")
SYSTEM_SECTIONS = ("演化日誌",)              # memory-audit 退役／還原時追加
_HEADER_RE = re.compile(r"^- ([A-Za-z][A-Za-z-]*):")
_MARK_RE = re.compile(r"^(\s*- )\[(臨|觀|固)\]")


# ─── 打包端 ──────────────────────────────────────────────────────────────────

def iter_shipped_sources(code_root: Path) -> Iterator[Tuple[str, Path]]:
    """(rel_path 'memory/…', 檔案) 依 rel_path 排序。"""
    mem = code_root / "memory"
    skip = SKIP_DIRS - {REFERENCE_DIR_NAME}
    found = []
    for p in mem.rglob("*.md"):
        parts = p.relative_to(mem).parts
        if len(parts) < 2 or parts[0].startswith("_"):
            continue
        if any(part in skip for part in parts[:-1]):
            continue
        if p.name.startswith("_") or p.name in (MEMORY_INDEX, ATOM_INDEX):
            continue
        found.append(("memory/" + "/".join(parts), p))
    yield from sorted(found)


def is_card_path(rel: str) -> bool:
    """出廠項是卡（進索引）還是參考文（_reference/）。"""
    return REFERENCE_DIR_NAME not in rel.split("/")


# ─── 指紋與合併 ──────────────────────────────────────────────────────────────

def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint(text: str) -> str:
    """去掉系統會自動改的部分再算 sha256（見檔頭說明）。"""
    out: List[str] = []
    in_header = True
    in_system_section = False
    for raw in text.lstrip("﻿").replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        if line.startswith("## "):
            in_header = False
            in_system_section = line[3:].strip() in SYSTEM_SECTIONS
        if in_system_section:
            continue
        m = _HEADER_RE.match(line)
        if in_header and m and m.group(1) in SYSTEM_HEADER_KEYS:
            continue
        out.append(_MARK_RE.sub(r"\1[]", line))
    return sha256_text("\n".join(out).strip("\n"))


def merge_system_fields(new_text: str, local_text: str) -> str:
    """換新版時保留本機的系統欄位：檔頭系統鍵用本機值；知識行內容相同的沿用本機的信心標記。"""
    local = local_text.lstrip("﻿").replace("\r\n", "\n").split("\n")
    local_header: Dict[str, str] = {}
    local_marks: Dict[str, str] = {}
    for line in local:
        m = _HEADER_RE.match(line)
        if m and m.group(1) in SYSTEM_HEADER_KEYS and m.group(1) not in local_header:
            local_header[m.group(1)] = line
        mm = _MARK_RE.match(line)
        if mm:
            local_marks[_MARK_RE.sub(r"\1[]", line.rstrip())] = mm.group(2)
    out: List[str] = []
    seen: set = set()
    in_header = True
    for line in new_text.split("\n"):
        if line.startswith("## "):
            if in_header:
                out = _insert_missing_header(out, local_header, seen)
            in_header = False
        m = _HEADER_RE.match(line)
        if in_header and m and m.group(1) in local_header:
            out.append(local_header[m.group(1)])
            seen.add(m.group(1))
            continue
        mm = _MARK_RE.match(line)
        if mm and not in_header:
            mark = local_marks.get(_MARK_RE.sub(r"\1[]", line.rstrip()))
            if mark:
                line = _MARK_RE.sub(lambda x: f"{x.group(1)}[{mark}]", line, count=1)
        out.append(line)
    return "\n".join(out)


def _insert_missing_header(out: List[str], local_header: Dict[str, str], seen: set) -> List[str]:
    """新版檔頭沒有、本機有的系統行（例 wg_provenance 補的 Source／Quote）補在檔頭最後一行 `- 鍵:` 後面。"""
    extra = [line for key, line in local_header.items() if key not in seen]
    if not extra:
        return out
    last = max((i for i, line in enumerate(out) if _HEADER_RE.match(line)), default=len(out) - 1)
    return out[:last + 1] + extra + out[last + 1:]


# ─── 展開端 ──────────────────────────────────────────────────────────────────

class ShippedError(Exception):
    """出廠包壞了或缺了：整段不做（不能把讀不到的卡當成「新版拿掉」去刪）。"""


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def load_shipped(code_root: Path) -> Tuple[dict, Dict[str, dict]]:
    """回 (manifest, {rel: {"sha256","text"}})；任何一包 sha 對不上或缺檔 → ShippedError。"""
    mem = code_root / "memory"
    mpath = mem / MANIFEST_NAME
    if not mpath.is_file():
        raise ShippedError(f"missing {mpath}")
    try:
        manifest = _read_json(mpath)
    except (OSError, ValueError) as e:
        raise ShippedError(f"unreadable {mpath}: {e}") from e
    entries: Dict[str, dict] = {}
    for pack in manifest.get("packs") or []:
        ppath = mem / str(pack.get("file") or "")
        try:
            raw = ppath.read_bytes()
        except OSError as e:
            raise ShippedError(f"unreadable {ppath}: {e}") from e
        if hashlib.sha256(raw).hexdigest() != pack.get("sha256"):
            raise ShippedError(f"sha mismatch {ppath}")
        for line in raw.decode("utf-8").split("\n"):
            if not line.strip():
                continue
            row = json.loads(line)
            if sha256_text(row["text"]) != row["sha256"]:
                raise ShippedError(f"card sha mismatch {row.get('path')}")
            entries[row["path"]] = {"sha256": row["sha256"], "text": row["text"]}
    if len(entries) != manifest.get("count"):
        raise ShippedError(f"count {len(entries)} != manifest {manifest.get('count')}")
    return manifest, entries


def _manifest_key(manifest: dict) -> str:
    return sha256_text(json.dumps({"version": manifest.get("version"), "packs": manifest.get("packs") or []}, sort_keys=True))


def _acquire_lock(path: Path) -> bool:
    try:
        if path.exists() and time.time() - path.stat().st_mtime > LOCK_STALE_SECS:
            path.unlink()
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except OSError:
        return False


def _empty_stats() -> dict:
    return {"fresh": 0, "adopted": 0, "updated": 0, "removed": 0, "kept_user": 0, "archived_skip": 0,
            "docs": 0, "kept_names": [], "changed": False, "skipped": "", "secs": 0.0, "version": ""}


def sync_shipped(code_root: Path, data_root: Path) -> dict:
    """展開／更新出廠卡，回統計 dict（見 _empty_stats）。changed=True 表示卡片檔有增刪改，呼叫端要重建索引。
    skipped：install（程式碼根＝資料根）／unchanged（manifest 沒變）／locked（別的 session 正在做）。出廠包壞了丟 ShippedError。"""
    t0 = time.perf_counter()
    stats = _empty_stats()
    if Path(os.path.realpath(code_root)) == Path(os.path.realpath(data_root)):
        stats["skipped"] = "install"
        return stats
    mem = data_root / "memory"
    state_path = mem / STATE_NAME
    try:
        state = _read_json(state_path) if state_path.is_file() else {}
    except (OSError, ValueError):
        state = {}   # 紀錄壞了：當沒展開過；磁碟上已有的卡走「收編／留著」，不會被覆寫
    mpath = code_root / "memory" / MANIFEST_NAME
    try:
        manifest_peek = _read_json(mpath)
    except (OSError, ValueError) as e:
        raise ShippedError(f"unreadable {mpath}: {e}") from e
    if state.get("manifest_key") == _manifest_key(manifest_peek):
        stats["skipped"] = "unchanged"
        return stats
    mem.mkdir(parents=True, exist_ok=True)
    lock = mem / LOCK_NAME
    if not _acquire_lock(lock):
        stats["skipped"] = "locked"
        return stats
    try:
        manifest, entries = load_shipped(code_root)
        records: Dict[str, dict] = dict(state.get("cards") or {})
        version = str(manifest.get("version") or "")
        stats["version"] = version
        for rel, ent in entries.items():
            _sync_one(mem, rel, ent, records, version, stats)
        for rel in sorted(set(records) - set(entries)):
            _remove_one(mem, rel, records, stats)
        new_state = {"version": version, "manifest_key": _manifest_key(manifest),
                     "cards": dict(sorted(records.items()))}
        _write_atomic(state_path, json.dumps(new_state, ensure_ascii=False, indent=1) + "\n")
    finally:
        try:
            lock.unlink()
        except OSError:
            pass
    stats["secs"] = round(time.perf_counter() - t0, 2)
    return stats


def _sync_one(mem: Path, rel: str, ent: dict, records: Dict[str, dict], version: str, stats: dict) -> None:
    path = mem.parent / rel
    rec = records.get(rel)
    is_card = is_card_path(rel)
    new_fp = fingerprint(ent["text"])
    if rec is None:
        if not path.exists():
            _write_atomic(path, ent["text"])
            stats["fresh" if is_card else "docs"] += 1
            stats["changed"] = True
            records[rel] = {"fp": new_fp, "sha": ent["sha256"], "ver": version}
            return
        local_fp = fingerprint(path.read_text(encoding="utf-8-sig"))
        if local_fp == new_fp:
            stats["adopted"] += 1
        else:
            _note_kept(stats, rel)
        records[rel] = {"fp": new_fp, "sha": ent["sha256"], "ver": version}
        return
    if rec.get("archived"):
        stats["archived_skip"] += 1
        return
    if not path.exists():
        rec["archived"] = True
        stats["archived_skip"] += 1
        return
    if rec.get("sha") == ent["sha256"]:
        return
    local_text = path.read_text(encoding="utf-8-sig")
    if fingerprint(local_text) != rec.get("fp"):
        _note_kept(stats, rel)
        rec["sha"] = ent["sha256"]   # 這一版報過了，下一版再報
        rec["ver"] = version
        return
    merged = merge_system_fields(ent["text"], local_text)
    _write_atomic(path, merged)
    stats["updated"] += 1
    stats["changed"] = True
    records[rel] = {"fp": fingerprint(merged), "sha": ent["sha256"], "ver": version}


def _remove_one(mem: Path, rel: str, records: Dict[str, dict], stats: dict) -> None:
    rec = records.pop(rel)
    path = mem.parent / rel
    if rec.get("archived") or not path.exists():
        return
    if fingerprint(path.read_text(encoding="utf-8-sig")) != rec.get("fp"):
        _note_kept(stats, rel)   # 使用者改過：留給他，從此不算出廠卡
        return
    path.unlink()
    sidecar = path.with_suffix(".access.json")   # 同 lib/atom_access._access_path
    if sidecar.exists():
        sidecar.unlink()
    stats["removed"] += 1
    stats["changed"] = True


def _note_kept(stats: dict, rel: str) -> None:
    stats["kept_user"] += 1
    stats["kept_names"].append(Path(rel).stem)
