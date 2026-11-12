#!/usr/bin/env python3
"""atom-set-realm.py — 把 atom 在 core ⇄ local 範疇間搬移（V5+ Realm 維度），以及把本地卡從舊位置整棵搬到新位置

local atom 物理落 `memory/_local/<domain>/`（舊位置 `_AIDocs/_atoms/<domain>/` 相容期只讀），但**索引仍 global、Scope 仍 global**
（realm 與 scope 正交；realm 由 index path 前綴推導、不存欄位、不寫 frontmatter）。

為何不用 atom-move.py：
  - atom-move（V5 SoT-correct 重寫後）走 JSON SoT、同根搬移保留 scope，但**刻意拒絕**
    本地樹（新位置 memory/_local/、舊位置 _AIDocs/_atoms/）下的 local atom（守門導回本工具）——realm 維度的 path 由本工具獨佔，
    防兩個寫者競爭翻轉 realm。
  - 本工具只搬實體檔（含 `.access.json` sidecar）+ 改 index path，Scope 一律不動（local 維持 global）。
  - 本工具為本地樹 index path 的**唯一寫者**（防 realm 翻轉）。

為何不需 reconcile 修反向連結：
  - global→local 是**純 path 搬移**（scope 不變、slug 不變）。Related 反向連結用 **slug**
    引用（非 path），slug 不變 → 連結不斷裂。`_ATOM_INDEX.md` mirror 由 upsert 自動重生。

可逆（memory-undo.py 只處理自動萃取 atom，不適用本搬移）：
  set <slug> --domain <World|Tools|MemDev>[/<子分類>…]   core → local（memory/ → memory/_local/<路徑>/；可多段，例 MemDev/寫碼守門閘；已在 local 的卡可換子分類）
  set <slug> --to-core [--category Lv1[/Lv2]] local → core（落 memory/<範疇>/；範疇經 core_write_target；
                                              `Failures/<主題>` 走失敗家族；寫入閘開後 --category 必填）
  set <slug> ... --dry-run                    只算路徑、不落檔

舊位置收尾（migrate-local；每場結束的自動收尾 hooks/wg_atoms_local_migrate.py 用同一份程式）：
  migrate-local --dry-run                     預覽：會搬幾張卡、幾個使用紀錄／投票檔、改幾列索引、刪幾份 _INDEX.md、有沒有衝突
  migrate-local [--undo-file <json>]          執行：_AIDocs/_atoms/** 的每個檔原樣搬到 memory/_local/ 的同一個相對路徑
                                              （卡、.access.json、_distant/ 封存、_votes/<人>.jsonl、_meta/feedback-votes-applied.json），
                                              改索引路徑、刪舊位置的 _INDEX.md 與空資料夾、重產目錄檔；落復原憑證
                                              plans/local-migrate-<時間>.undo.json
  migrate-local --undo <json> [--dry-run]     照復原憑證整批搬回去（被併過、被刪掉的檔用憑證裡的原文還原）
  migrate-local ... --no-sync-index           不重產 MEMORY.md、_local_catalog.md、各層 _INDEX.md（之後自己跑 sync-memory-index --write）
  新位置已有同一個相對路徑的檔：卡（.md）或併不了的檔 → 衝突（撞名），整批停下、列出、不覆蓋；使用紀錄兩份計數相加、
  投票檔逐行合併、已套清單取聯集、位元組相同就刪舊的那份。重複跑：搬完再跑一次什麼都不動。

sidecar 隨 .md 原子性搬：先搬 .md，sidecar 搬失敗則 rollback .md，
避免 confirmations/usefulness 計數歸零、晉升歷史飄移。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

_CODE_ROOT = Path(__file__).resolve().parent.parent   # 程式碼根：lib.* 從本樹載
if str(_CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(_CODE_ROOT))

# CLAUDE_DIR＝使用者資料根（index path 的基準），和 lib 同一份；_CLAUDE_ROOT 是同一個值，搬移落點算相對路徑用
from lib.atom_locations import (  # noqa: E402
    CLAUDE_DIR, CLAUDE_DIR as _CLAUDE_ROOT, FAILURES_ROOT_NAME,
    GLOBAL_MEMORY_DIR, LOCAL_ATOMS_DIR, LOCAL_ATOMS_REL, LOCAL_REALM_DOMAINS,
    LEGACY_LOCAL_ATOMS_DIR, LEGACY_LOCAL_ATOMS_REL,
    core_write_target, enumerate_local_paths, failures_write_target,
    is_local_realm_path, local_realm_path_segments, normalize_domain_path, unclassified_error,
    ENTRY_BOUND_STAYS_CORE, entry_bound_ids,
)
from lib.i18n import t  # noqa: E402
try:
    from lib.atom_taxonomy import core_categories as _core_categories  # noqa: E402
    from lib.atom_taxonomy import gate_enabled as _gate_enabled  # noqa: E402
except Exception:  # taxonomy 缺 → 閘視為關（flat 落點仍可用）
    _core_categories = None
    _gate_enabled = None
from lib.atom_io import write_index, write_text_lf, _audit_log, _gen_audit_id  # noqa: E402
from lib.atom_index_json import load_atom_index_json, retry_busy, save_index_with_mirror  # noqa: E402
from lib.atom_access import (  # noqa: E402
    access_sidecar_path, merge_access_records, move_atom_pair, prune_empty_parents,
)
from lib.wg_platform import no_window_flags  # noqa: E402

_SOURCE = "tool:atom-set-realm"


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _find_index_entry(slug: str) -> Optional[Dict[str, Any]]:
    data = load_atom_index_json(GLOBAL_MEMORY_DIR)
    for a in data.get("atoms", []):
        if a.get("name") == slug:
            return a
    return None


def _locate_md(slug: str, index_path: str) -> Optional[Path]:
    """以 index path（相對 CLAUDE_DIR）優先定位；落空則 rglob memory/（含新本地樹）+ 舊本地樹 _AIDocs/_atoms/。"""
    if index_path:
        p = CLAUDE_DIR / index_path
        if p.exists():
            return p
    for root in (GLOBAL_MEMORY_DIR, LEGACY_LOCAL_ATOMS_DIR):
        if root.is_dir():
            for hit in root.rglob(f"{slug}.md"):
                return hit
    return None


def _local_root_of(md: Path) -> Path:
    """md 所在的本地樹根（舊位置 _AIDocs/_atoms 或新位置 memory/_local）：搬離後清空資料夾止於這一層。"""
    try:
        if LEGACY_LOCAL_ATOMS_DIR.resolve() in Path(md).resolve().parents:
            return LEGACY_LOCAL_ATOMS_DIR
    except OSError:
        pass
    return LOCAL_ATOMS_DIR


# sidecar-aware 原子搬移 helper 已上移到 lib.atom_access（atom-move / atom-set-realm 共用單一來源）：
#   access_sidecar_path / move_atom_pair / prune_empty_parents（行為不變，prune 的 stop 由 _local_root_of 決定）


def _read_scope(md: Path) -> str:
    """讀 .md frontmatter 的 Scope（搬移不改它，僅供回報/驗證 scope 仍 global）。"""
    try:
        text = md.read_text(encoding="utf-8-sig")
    except OSError:
        return "?"
    import re
    m = re.search(r"^-\s*Scope:\s*(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else "?"


# ─── Core operation ───────────────────────────────────────────────────────────


def _core_landing(slug: str, category: Optional[str]) -> tuple:
    """--to-core 的落點：(dst_md, new_rel, error)。

    `category` 給了 → 經 core_write_target（Lv1 閉合清單、別名 snap、Lv2 自由）落
    memory/<Lv1>[/<Lv2>]/；`Failures/<主題>` 走 failures_write_target。沒給 → 寫入閘
    （taxonomy.gate_enabled）開時拒、關時落 memory/ 根（遷移期相容）。
    """
    raw = (category or "").strip().replace("\\", "/")
    if raw:
        head, _, rest = raw.partition("/")
        if head.casefold() == FAILURES_ROOT_NAME.casefold():
            t = failures_write_target(rest or None)
        else:
            t, err = core_write_target(raw, allow_new=False)
            if err:
                return (None, "", err)
        dst = Path(t["dir"]) / f"{slug}.md"
        return (dst, dst.relative_to(_CLAUDE_ROOT).as_posix(), None)
    gate_on = False
    if _gate_enabled is not None:
        try:
            gate_on = bool(_gate_enabled())
        except Exception:
            gate_on = False
    if gate_on:
        cats: List[str] = []
        if _core_categories is not None:
            try:
                cats = list(_core_categories())
            except Exception:
                cats = []
        return (None, "", unclassified_error(category, cats) + " (pass --category <Lv1[/Lv2]>)")
    dst = GLOBAL_MEMORY_DIR / f"{slug}.md"
    return (dst, f"memory/{slug}.md", None)


def set_realm(
    slug: str, *, domain: Optional[str] = None, to_core: bool = False,
    dry_run: bool = False, category: Optional[str] = None,
) -> Dict[str, Any]:
    """把 atom 搬到 local（--domain）或搬回 core（--to_core [--category]）。

    回 {ok, ...}；no-op（已在目標 realm）回 {ok:True, noop:True}。
    Scope 一律保持原值（必為 global），不修改 .md 的 Scope 行。
    --to-core 的範疇落點見 _core_landing。local 落點只寫新位置 memory/_local/（舊位置的卡換子分類時也搬到新位置）。
    """
    entry = _find_index_entry(slug)
    if not entry:
        return {"ok": False, "error": f"atom not in index: {slug}"}
    cur_path = entry.get("path", "")
    triggers: List[str] = entry.get("triggers", [])
    scope = entry.get("scope", "global")

    src_md = _locate_md(slug, cur_path)
    if not src_md or not src_md.exists():
        return {"ok": False, "error": f"atom .md not found on disk: {slug} (index path={cur_path})"}

    cur_is_local = is_local_realm_path(cur_path)

    if to_core:
        if not cur_is_local:
            return {"ok": True, "noop": True, "msg": f"{slug} already core ({cur_path})"}
        dst_md, new_rel, err = _core_landing(slug, category)
        if err:
            return {"ok": False, "error": err}
        new_realm = "core"
    else:
        if not (domain or "").strip():
            return {"ok": False, "error": "empty domain (need --domain <path> or --to-core)"}
        # 多段階層路徑：canon（對既有樹 snap）取代舊 allow-list 驗證
        dom_path = normalize_domain_path(domain, enumerate_local_paths())
        # 已在 local 的卡：同一夾 → noop；不同夾 → 照搬（local 內換子夾，realm 不變）
        if cur_is_local and "/".join(local_realm_path_segments(cur_path)).casefold() == dom_path.casefold():
            return {"ok": True, "noop": True, "msg": f"{slug} already local at {cur_path}"}
        segs = [s for s in dom_path.split("/") if s]
        dst_md = LOCAL_ATOMS_DIR.joinpath(*segs, f"{slug}.md")
        new_rel = f"{LOCAL_ATOMS_REL}/{dom_path}/{slug}.md"
        new_realm = "local"

    if dst_md.resolve() == src_md.resolve():
        return {"ok": True, "noop": True, "msg": f"{slug} already at {new_rel}"}
    if dst_md.exists():
        return {"ok": False, "error": f"target already exists: {dst_md}"}

    cur_scope_field = _read_scope(src_md)
    # ENTRY_BOUND_STAYS_CORE：自動搬移會擋；手動搬是人判斷過的動作，只警告、照搬
    entry_ids = entry_bound_ids(entry, src_md) if new_realm == "local" else []
    warn = ({"warning": t("cli.atom_set_realm.entry_bound_warning", code=ENTRY_BOUND_STAYS_CORE, slug=slug, entries=", ".join(entry_ids))}
            if entry_ids else {})

    if dry_run:
        if to_core:  # core_write_target 已 mkdir 落點：dry-run 不留空目錄鏈
            prune_empty_parents(dst_md.parent, GLOBAL_MEMORY_DIR)
            try:
                dst_md.parent.rmdir()
            except OSError:
                pass
        return {
            "ok": True, "dry_run": True, "slug": slug,
            "from": cur_path, "to": new_rel,
            "from_realm": "local" if cur_is_local else "core", "to_realm": new_realm,
            "scope": scope, "scope_field": cur_scope_field,
            "sidecar": access_sidecar_path(src_md).exists(), **warn,
        }

    # 1) 實體檔 + sidecar 原子搬
    try:
        sidecar_moved = move_atom_pair(src_md, dst_md)
    except OSError as e:
        return {"ok": False, "error": f"move failed: {e}"}

    # 2) 更新 index path（write_index → upsert，scope 強制 global、走 audit）
    res = write_index(GLOBAL_MEMORY_DIR, slug, new_rel, triggers, _SOURCE)
    if not res.ok:
        # rollback：.md + sidecar 搬回原處
        try:
            dst_md.rename(src_md)
            if sidecar_moved:
                access_sidecar_path(dst_md).rename(access_sidecar_path(src_md))
        except OSError:
            pass
        return {"ok": False, "error": f"index update failed: {res.error}"}

    # 3) 專屬 realm_move audit（可追溯 + 反向 undo 依據）
    _audit_log({
        "audit_id": _gen_audit_id(),
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "op": "realm_move", "source": _SOURCE, "slug": slug,
        "from_path": cur_path, "to_path": new_rel,
        "from_realm": "local" if cur_is_local else "core", "to_realm": new_realm,
        "sidecar_moved": sidecar_moved,
    })

    # 4) 搬離 local 子夾後清空的階層目錄（best-effort，止於卡原本所在的本地樹根：新位置或舊位置）
    if cur_is_local:
        prune_empty_parents(src_md.parent, _local_root_of(src_md))

    return {
        "ok": True, "slug": slug, "from": cur_path, "to": new_rel,
        "to_realm": new_realm, "scope": scope, "scope_field": cur_scope_field,
        "sidecar_moved": sidecar_moved, "md_path": str(dst_md), **warn,
    }


# ─── 舊位置收尾：本地卡從 _AIDocs/_atoms/ 整棵平移到 memory/_local/（migrate-local）──────────
#
# 每個檔照「相對本地樹根的路徑」原樣搬，分類、檔名、層數都不變，卡片之間的連結（用卡名）與投票的 path_rel
# （相對本地樹根）都不用改。只有索引的 path 換開頭。舊位置各分類層的 _INDEX.md 由 sync-memory-index 產生：
# 舊的刪掉、新的在新位置重產；底線開頭資料夾（_distant/ 等）裡的 _INDEX.md 是手寫的，照一般檔搬。
# hooks/wg_atoms_local_migrate.py（每場結束）呼叫同一組函式，另帶兩個限制：只搬殘留（residue_only）、
# 別的 session 開場快取還指著的卡不搬（held）。

UNDO_DIR = CLAUDE_DIR / "plans"            # 復原憑證（.gitignore 已排除 plans/），同 atom-categorize
UNDO_PREFIX = "local-migrate-"
INDEX_MD_NAME = "_INDEX.md"
SIDECAR_SUFFIX = ".access.json"
VOTES_DIR_NAME = "_votes"
APPLIED_REL = "_meta/feedback-votes-applied.json"
SYNC_INDEX_TIMEOUT_S = 60
STATE_GLOB = "state-*.json"


def _legacy_rest(rel_path: str) -> Optional[str]:
    """索引路徑在舊位置 → 去掉舊開頭後的相對路徑；否則 None。"""
    prefix = LEGACY_LOCAL_ATOMS_REL + "/"
    return rel_path[len(prefix):] if rel_path.startswith(prefix) else None


def _new_rel(rest: str) -> str:
    return f"{LOCAL_ATOMS_REL}/{rest}"


def _is_regenerable_index_md(rel: str) -> bool:
    """各分類層的 _INDEX.md（sync-memory-index 產的）→ True；底線開頭資料夾裡的是手寫的 → False（照一般檔搬）。"""
    parts = rel.split("/")
    return parts[-1] == INDEX_MD_NAME and not any(p.startswith("_") for p in parts[:-1])


def _blob(data: bytes) -> Dict[str, str]:
    """檔案原文放進復原憑證：UTF-8 讀得出來就存文字（人看得懂），否則 base64。"""
    try:
        return {"text": data.decode("utf-8")}
    except UnicodeDecodeError:
        return {"b64": base64.b64encode(data).decode("ascii")}


def _unblob(blob: Dict[str, str]) -> bytes:
    if "text" in blob:
        return blob["text"].encode("utf-8")
    return base64.b64decode(blob.get("b64", ""))


def _write_bytes(path: Path, data: bytes) -> None:
    """位元組原樣落檔（tmp + replace）：還原要逐位元組一樣，不走會改換行的 write_text_lf。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _vote_key(line: str) -> str:
    try:
        vid = json.loads(line).get("id")
        return f"id:{vid}" if vid else f"line:{line}"
    except (ValueError, AttributeError):
        return f"line:{line}"


def _merge_votes(legacy: bytes, dest: bytes) -> bytes:
    """投票檔（每行一票、只追加）：舊位置的行在前、新位置的在後，同一票（同 id）只留一次。"""
    out: List[str] = []
    seen: Set[str] = set()
    for blob in (legacy, dest):
        for line in blob.decode("utf-8").splitlines():
            if not line.strip():
                continue
            key = _vote_key(line)
            if key in seen:
                continue
            seen.add(key)
            out.append(line)
    return ("\n".join(out) + "\n").encode("utf-8") if out else b""


def _merge_applied(legacy: bytes, dest: bytes) -> bytes:
    """已套清單（JSON 陣列，票 id）：取聯集，格式同 lib/atom_feedback._save_applied（排序、不轉義中文）。"""
    ids = {str(x) for blob in (legacy, dest) for x in json.loads(blob.decode("utf-8"))}
    return json.dumps(sorted(ids), ensure_ascii=False).encode("utf-8")


def _merge_sidecar(legacy: bytes, dest: bytes) -> bytes:
    return json.dumps(merge_access_records(json.loads(legacy.decode("utf-8")), json.loads(dest.decode("utf-8"))),
                      ensure_ascii=False).encode("utf-8")


_MERGERS = {"merge_sidecar": _merge_sidecar, "merge_votes": _merge_votes, "merge_applied": _merge_applied}


def _merge_kind(rel: str) -> Optional[str]:
    if rel.endswith(SIDECAR_SUFFIX):
        return "merge_sidecar"
    head, _, tail = rel.partition("/")
    if head == VOTES_DIR_NAME and tail.endswith(".jsonl") and "/" not in tail:
        return "merge_votes"
    if rel == APPLIED_REL:
        return "merge_applied"
    return None


def _file_action(rel: str, src: Path, dst: Path) -> Tuple[Optional[str], Optional[str]]:
    """非卡片的檔要怎麼搬：(action, 衝突原因)。新位置沒有 → move；位元組相同 → dedupe；
    使用紀錄／投票檔／已套清單 → 併（兩份都讀得懂才併）；其餘兩邊都有 → 衝突。"""
    if not dst.exists():
        return "move", None
    try:
        a, b = src.read_bytes(), dst.read_bytes()
    except OSError as e:
        return None, f"unreadable: {e}"
    if a == b:
        return "dedupe", None
    kind = _merge_kind(rel)
    if kind is None:
        return None, "exists at both locations"
    try:
        _MERGERS[kind](a, b)
    except (ValueError, TypeError, AttributeError, UnicodeDecodeError) as e:
        return None, f"cannot merge ({kind}): {type(e).__name__}"
    return kind, None


def paths_held_by_sessions(workflow_dir: Path, exclude: str = "") -> Tuple[Dict[str, List[str]], List[str]]:
    """別的 session 開場快取（workflow/state-<sid>.json 的 atom_index.global）裡還指著舊位置的路徑。

    回 ({舊位置索引路徑: [session id…]}, [讀不到的 session id…])。注入時照快取裡的路徑讀卡、讀不到就安靜跳過
    （hooks/wg_atoms_budget.load_atoms_within_budget），compact／resume 專案根指紋沒變也沿用這份快取，所以還開著、
    或收尾了還能續接的 session 都算（state 檔由 _cleanup_old_states 照分級 TTL 清，最長 7 天）。exclude＝自己這一場。
    被別的行程占著讀不到 → 列進第二個回傳值，呼叫端當成「全部都還在用」。
    """
    held: Dict[str, List[str]] = {}
    unreadable: List[str] = []
    prefix = LEGACY_LOCAL_ATOMS_REL + "/"
    try:
        files = sorted(Path(workflow_dir).glob(STATE_GLOB))
    except OSError:
        return held, unreadable
    for fp in files:
        sid = fp.stem[len("state-"):]
        if exclude and sid == exclude:
            continue
        try:
            text = retry_busy(lambda: fp.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except OSError:
            unreadable.append(sid)
            continue
        try:
            state = json.loads(text)
        except ValueError:
            continue                       # 壞檔：不是活的快取
        if not isinstance(state, dict) or state.get("merged_into"):
            continue                       # 不是 session state（擴充同名檔），或已併進別的 session
        pool = (state.get("atom_index") or {}).get("global") or []
        for row in pool if isinstance(pool, list) else []:
            path = row[1] if isinstance(row, (list, tuple)) and len(row) > 1 else None
            if isinstance(path, str) and path.startswith(prefix):
                held.setdefault(path, []).append(sid)
    return held, unreadable


def plan_local_migration(*, held: Optional[Dict[str, List[str]]] = None, hold_all: bool = False,
                         residue_only: bool = False) -> Dict[str, Any]:
    """算出舊位置收尾要做什麼，不動任何檔。

    held：{舊位置索引路徑: [session…]}，這些卡這次不搬（連同它的使用紀錄）；hold_all＝所有有索引的卡都不搬。
    residue_only（每場結束的自動收尾用）：本機索引裡舊位置的本地卡比新位置的多＝整套版面還在舊位置
    （作者機手動搬家之前、程式先到卡還沒到的機器）→ 卡、投票檔與其他檔都不動（留給手動 migrate-local：預覽、量、一次提交），
    只做「卡已在新位置、使用紀錄還在舊位置」的配對與索引修正；反過來（新位置已是多數）才是拉到新版面後的殘留，照搬。
    """
    held = held or {}
    legacy, new = LEGACY_LOCAL_ATOMS_DIR, LOCAL_ATOMS_DIR
    data = load_atom_index_json(GLOBAL_MEMORY_DIR, strict=True)   # 被別的行程鎖著讀不到 → 丟 OSError，不當成空索引
    if not data.get("atoms") and legacy.is_dir() and any(legacy.rglob("*.md")):
        # 索引讀成 0 筆（壞了或不在）還照搬，有索引的卡會被當成沒索引的卡搬走、索引卻還指舊位置
        raise RuntimeError("index _atom_index.json is empty or unreadable; refusing to move local cards "
                           "(rebuild it first: tools/sync-atom-index.py)")
    legacy_rows: Dict[str, Dict[str, Any]] = {}      # 相對路徑 casefold → 索引列
    new_indexed = 0
    for a in data.get("atoms", []):
        if not isinstance(a, dict):
            continue
        path = a.get("path") or ""
        rest = _legacy_rest(path)
        if rest is not None:
            legacy_rows[rest.casefold()] = a
        elif is_local_realm_path(path):
            new_indexed += 1

    files = sorted(p for p in legacy.rglob("*") if p.is_file()) if legacy.is_dir() else []
    rels = [p.relative_to(legacy).as_posix() for p in files]
    relset = set(rels)
    folded = {r.casefold(): r for r in rels}
    legacy_indexed = sum(1 for k in legacy_rows if k in folded and folded[k].endswith(".md"))
    keep_layout = residue_only and legacy_indexed > new_indexed

    items: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, str]] = []
    held_out: List[Dict[str, Any]] = []
    index_rewrites: List[Dict[str, Any]] = []
    deferred = 0
    index_md: List[str] = []
    for rel in rels:
        src, dst = legacy / rel, new / rel
        if _is_regenerable_index_md(rel):
            index_md.append(rel)
            continue
        if rel.endswith(SIDECAR_SUFFIX) and (rel[:-len(SIDECAR_SUFFIX)] + ".md") in relset:
            continue                       # 跟著自己的卡一起搬（下面 .md 那條）
        if rel.endswith(SIDECAR_SUFFIX):   # 卡不在舊位置：已在新位置（git 搬過去了）或哪裡都沒有 → 配回去／原樣搬
            card_at_new = (new / (rel[:-len(SIDECAR_SUFFIX)] + ".md")).exists()
            if keep_layout and not card_at_new:
                deferred += 1
                continue
            action, why = _file_action(rel, src, dst)
            if why:
                conflicts.append({"rel": rel, "reason": why})
                continue
            items.append({"kind": "sidecar", "rel": rel, "action": action, "card_at_new": card_at_new})
            continue
        if keep_layout:
            deferred += 1
            continue
        if not rel.endswith(".md"):
            kind = "data" if _merge_kind(rel) in ("merge_votes", "merge_applied") else "other"
            action, why = _file_action(rel, src, dst)
            if why:
                conflicts.append({"rel": rel, "reason": why})
                continue
            items.append({"kind": kind, "rel": rel, "action": action})
            continue
        row = legacy_rows.get(rel.casefold())
        if row is not None and (hold_all or row.get("path") in held):
            held_out.append({"name": row.get("name"), "path": row.get("path"),
                             "sessions": sorted(set(held.get(row.get("path"), [])))})
            continue
        if dst.exists():
            conflicts.append({"rel": rel, "reason": "card exists at both locations"})
            continue
        sidecar = None
        if access_sidecar_path(src).exists():
            s_rel = rel[:-len(".md")] + SIDECAR_SUFFIX
            sidecar, why = _file_action(s_rel, access_sidecar_path(src), access_sidecar_path(dst))
            if why:
                conflicts.append({"rel": s_rel, "reason": why})
                continue
        items.append({"kind": "card", "rel": rel, "indexed": row is not None, "sidecar": sidecar})
        if row is not None:
            index_rewrites.append({"name": row.get("name"), "from": row.get("path"), "to": _new_rel(rel)})

    missing: List[Dict[str, Any]] = []
    for key, row in legacy_rows.items():
        if key in folded:
            continue
        rest = _legacy_rest(row.get("path") or "") or ""
        if (new / rest).exists():          # 卡已在新位置、索引還指舊位置 → 只改索引
            index_rewrites.append({"name": row.get("name"), "from": row.get("path"), "to": _new_rel(rest),
                                   "fixup": True})
        else:
            missing.append({"name": row.get("name"), "path": row.get("path")})

    moving = {it["rel"] for it in items}
    moving |= {it["rel"][:-len(".md")] + SIDECAR_SUFFIX for it in items if it["kind"] == "card" and it.get("sidecar")}
    left = [r for r in rels if r not in moving and r not in index_md]

    def _emptied(index_rel: str) -> bool:   # 這份 _INDEX.md 的資料夾底下搬完不剩別的檔 → 會被刪
        folder = index_rel.rsplit("/", 1)[0] + "/" if "/" in index_rel else ""
        return not any(x.startswith(folder) for x in left)

    index_md_removable = [r for r in index_md if _emptied(r)]

    counts = {
        "cards": sum(1 for it in items if it["kind"] == "card"),
        "indexed_cards": sum(1 for it in items if it["kind"] == "card" and it["indexed"]),
        "unindexed_cards": sum(1 for it in items if it["kind"] == "card" and not it["indexed"]),
        "sidecars_with_cards": sum(1 for it in items if it["kind"] == "card" and it.get("sidecar")),
        "paired_sidecars": sum(1 for it in items if it["kind"] == "sidecar" and it["card_at_new"]),
        "orphan_sidecars": sum(1 for it in items if it["kind"] == "sidecar" and not it["card_at_new"]),   # 卡哪裡都沒有：原樣搬
        "data_files": sum(1 for it in items if it["kind"] == "data"),
        "other_files": sum(1 for it in items if it["kind"] == "other"),
        "merges": sum(1 for it in items if str(it.get("action") or it.get("sidecar") or "").startswith("merge")),
        "dedupes": sum(1 for it in items if (it.get("action") or it.get("sidecar")) == "dedupe"),
        "index_rewrites": len(index_rewrites),
        "index_fixups": sum(1 for r in index_rewrites if r.get("fixup")),
        "legacy_index_md": len(index_md_removable),
        "conflicts": len(conflicts), "held": len(held_out), "deferred": deferred, "missing": len(missing),
    }
    return {
        "legacy_root": LEGACY_LOCAL_ATOMS_REL, "new_root": LOCAL_ATOMS_REL,
        "legacy_exists": legacy.is_dir(), "keep_layout": keep_layout,
        "local_indexed": {"legacy": legacy_indexed, "new": new_indexed},
        "counts": counts, "items": items, "index_rewrites": index_rewrites,
        "legacy_index_md": index_md_removable, "conflicts": conflicts, "held": held_out, "missing": missing,
    }


def _rewrite_index_paths(rows: List[Dict[str, Any]], *, reverse: bool = False) -> List[str]:
    """索引裡這些列的 path 一次改完、一次落檔（只換 path，其他欄位一字不動；apply_upsert 會丟掉 last_used 這類欄，不用它）。

    已經在目標路徑的列略過（重跑、回滾一半都安全）；既不在起點也不在目標的列＝搬的時候索引被別人改過：
    正向整批不寫、丟 RuntimeError（呼叫端回滾）；反向（復原）照改其他列，回這些卡名。
    落檔只失敗在 _ATOM_INDEX.md 鏡像（lib 已在 stderr 說）：JSON 是真相、已寫好，不算失敗。
    """
    if not rows:
        return []
    data = load_atom_index_json(GLOBAL_MEMORY_DIR, strict=True)
    by_name = {a.get("name"): a for a in data.get("atoms", []) if isinstance(a, dict)}
    frm, to = ("to", "from") if reverse else ("from", "to")
    todo, stale = [], []
    for r in rows:
        cur = (by_name.get(r["name"]) or {}).get("path")
        if cur == r[frm]:
            todo.append(r)
        elif cur != r[to]:
            stale.append(r["name"])
    if stale and not reverse:
        raise RuntimeError(f"index changed during the move (rows no longer at the expected path): {stale[:5]}")
    if not todo:
        return stale
    for r in todo:
        by_name[r["name"]]["path"] = r[to]
    try:
        save_index_with_mirror(GLOBAL_MEMORY_DIR, data)
    except OSError:
        now = {a.get("name"): a.get("path") for a in load_atom_index_json(GLOBAL_MEMORY_DIR).get("atoms", [])
               if isinstance(a, dict)}
        if any(now.get(r["name"]) != r[to] for r in todo):
            raise
    return stale


def _apply_item(it: Dict[str, Any], done: List[Dict[str, Any]]) -> None:
    rel = it["rel"]
    src, dst = LEGACY_LOCAL_ATOMS_DIR / rel, LOCAL_ATOMS_DIR / rel
    if it["kind"] == "card":
        s_rel = rel[:-len(".md")] + SIDECAR_SUFFIX
        if it.get("sidecar") in ("merge_sidecar", "dedupe"):   # 新位置已有使用紀錄：先併（或刪掉一樣的那份），再搬卡
            _apply_file(s_rel, it["sidecar"], done)
        if dst.exists():
            raise OSError(f"target appeared during the move: {dst}")
        moved = move_atom_pair(src, dst)
        done.append({"op": "move_card", "rel": rel, "sidecar_moved": moved})
        return
    _apply_file(rel, it["action"], done)


def _apply_file(rel: str, action: str, done: List[Dict[str, Any]]) -> None:
    src, dst = LEGACY_LOCAL_ATOMS_DIR / rel, LOCAL_ATOMS_DIR / rel
    if action == "move":
        if dst.exists():
            raise OSError(f"target appeared during the move: {dst}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        done.append({"op": "move", "rel": rel})
        return
    before = src.read_bytes()
    if action == "dedupe":
        src.unlink()
        done.append({"op": "dedupe", "rel": rel, "legacy": _blob(before)})
        return
    dest_before = dst.read_bytes()
    _write_bytes(dst, _MERGERS[action](before, dest_before))
    src.unlink()
    done.append({"op": "merge", "rel": rel, "legacy": _blob(before), "dest_before": _blob(dest_before)})


def _revert_entry(e: Dict[str, Any]) -> None:
    """復原憑證的一步反過來做（執行失敗時回滾、--undo 時整批搬回去都用這支）。"""
    op = e["op"]
    if op == "index":
        stale = _rewrite_index_paths(e["rows"], reverse=True)
        if stale:
            raise RuntimeError(f"index rows changed since the move, left as they are: {stale[:5]}")
        return
    rel = e["rel"]
    src, dst = LEGACY_LOCAL_ATOMS_DIR / rel, LOCAL_ATOMS_DIR / rel
    if op == "rmdir":
        src.mkdir(parents=True, exist_ok=True)
    elif op == "delete":   # 可重產的 _INDEX.md：舊位置若已被重產出一份，照憑證原文蓋回去
        _write_bytes(src, _unblob(e["content"]))
    elif op == "move_card":
        if src.exists():
            raise OSError(f"original path occupied: {src}")
        if not dst.exists():
            raise OSError(f"expected at {dst} (not found; already reverted?)")
        if e.get("sidecar_moved"):
            move_atom_pair(dst, src)
        else:
            src.parent.mkdir(parents=True, exist_ok=True)
            dst.rename(src)
    elif op == "move":
        if src.exists():
            raise OSError(f"original path occupied: {src}")
        if not dst.exists():
            raise OSError(f"expected at {dst} (not found; already reverted?)")
        src.parent.mkdir(parents=True, exist_ok=True)
        dst.rename(src)
    elif op == "dedupe":
        _write_bytes(src, _unblob(e["legacy"]))
    elif op == "merge":
        _write_bytes(src, _unblob(e["legacy"]))
        _write_bytes(dst, _unblob(e["dest_before"]))
    else:
        raise ValueError(f"unknown undo op: {op}")


def _revert_all(entries: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """倒著一步一步復原；回 (沒復原成的步驟, 錯誤訊息)。"""
    left: List[Dict[str, Any]] = []
    errors: List[str] = []
    for e in reversed(entries):
        try:
            _revert_entry(e)
        except (OSError, ValueError, RuntimeError) as ex:
            left.insert(0, e)
            errors.append(f"{e.get('op')} {e.get('rel', '')}: {ex}".strip())
    return left, errors


def _tidy_tree(root: Path, done: Optional[List[Dict[str, Any]]], stop: Path) -> Tuple[int, List[str]]:
    """root 底下由深到淺：只剩一份可重產 _INDEX.md 的資料夾刪掉那份（原文記進 done）、空資料夾刪掉；
    root 自己最後處理，再往上清空資料夾到 stop（不含）。只碰 _INDEX.md，其他檔一律不刪。
    回 (刪掉幾份 _INDEX.md, 刪不掉的錯誤)；清理失敗不算搬家失敗（卡都已到位），錯誤交呼叫端回報。"""
    if not root.is_dir():
        return 0, []
    removed, errors = 0, []
    dirs = sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True)
    for d in [*dirs, root]:
        try:
            entries = list(d.iterdir())
            lone = entries[0] if len(entries) == 1 else None
            if (lone is not None and lone.name == INDEX_MD_NAME and lone.is_file()
                    and _is_regenerable_index_md(lone.relative_to(root).as_posix())):
                content = lone.read_bytes()
                lone.unlink()
                if done is not None:
                    done.append({"op": "delete", "rel": lone.relative_to(root).as_posix(), "content": _blob(content)})
                removed += 1
                entries = []
        except OSError as e:
            errors.append(f"{d}: {e}")
            continue
        if not entries and d != root:
            prune_empty_parents(d, d.parent)
            if done is not None and not d.exists():   # 刪掉的資料夾也記：搬家前就空著的（例如 Else/），復原時才建得回來
                done.append({"op": "rmdir", "rel": d.relative_to(root).as_posix()})
    prune_empty_parents(root, stop)
    return removed, errors


def _sync_memory_index() -> Optional[str]:
    """重產 MEMORY.md／_local_catalog.md／各層 _INDEX.md（tools/sync-memory-index.py --write）；成功回 None，失敗回原因。"""
    cmd = [sys.executable, str(_CODE_ROOT / "tools" / "sync-memory-index.py"), "--write"]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              cwd=str(CLAUDE_DIR), timeout=SYNC_INDEX_TIMEOUT_S, env=env,
                              creationflags=no_window_flags())
    except subprocess.TimeoutExpired:
        return f"sync-memory-index timeout ({SYNC_INDEX_TIMEOUT_S}s)"
    except OSError as e:
        return f"sync-memory-index failed to start: {e}"
    if proc.returncode == 0:
        return None
    tail = (proc.stderr or "").strip().splitlines()[-3:]
    return f"sync-memory-index rc={proc.returncode}: {' | '.join(tail) or '(no stderr)'}"


def _undo_path(undo_file: Optional[Path]) -> Path:
    if undo_file:
        return Path(undo_file)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = UNDO_DIR / f"{UNDO_PREFIX}{stamp}.undo.json"
    n = 2
    while path.exists():
        path = UNDO_DIR / f"{UNDO_PREFIX}{stamp}-{n}.undo.json"
        n += 1
    return path


def _write_undo(path: Path, entries: List[Dict[str, Any]], origin: str, counts: Dict[str, int]) -> None:
    payload = {"created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source": _SOURCE,
               "kind": "migrate-local", "origin": origin, "legacy_root": LEGACY_LOCAL_ATOMS_REL,
               "new_root": LOCAL_ATOMS_REL, "counts": counts, "entries": entries}
    write_text_lf(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def apply_local_migration(plan: Dict[str, Any], *, origin: str = "cli", undo_file: Optional[Path] = None,
                          sync_index: bool = True) -> Dict[str, Any]:
    """照 plan 搬。有衝突整批拒跑；半途失敗把已做的倒回去（同 set_realm 的回滾），倒不回的步驟落成復原憑證。
    成功：改索引（一次落檔）→ 每張卡一行 local_relocate 帳 → 清舊位置的 _INDEX.md 與空資料夾 → 復原憑證 → 重產目錄檔。"""
    if plan["conflicts"]:
        return {"ok": False, "refused": "conflicts", "applied": 0, "conflicts": plan["conflicts"]}
    done: List[Dict[str, Any]] = []
    try:
        for it in plan["items"]:
            _apply_item(it, done)
        if plan["index_rewrites"]:
            _rewrite_index_paths(plan["index_rewrites"])
            done.append({"op": "index", "rows": plan["index_rewrites"]})
    except (OSError, ValueError, RuntimeError) as e:
        left, errors = _revert_all(done)
        out: Dict[str, Any] = {"ok": False, "error": f"{type(e).__name__}: {e}", "applied": 0,
                               "rolled_back": len(done) - len(left), "rollback_errors": errors}
        if left:
            path = _undo_path(undo_file)
            _write_undo(path, left, origin, plan["counts"])
            out["undo_file"] = str(path)
        return out

    removed, tidy_errors = _tidy_tree(LEGACY_LOCAL_ATOMS_DIR, done, CLAUDE_DIR)
    if all(e["op"] == "rmdir" for e in done):   # 什麼都沒搬（頂多清掉空資料夾）：不落憑證、不重產目錄檔
        return {"ok": True, "applied": 0, "undo_file": None, "sync_error": None, "tidy_errors": tidy_errors}
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for r in plan["index_rewrites"]:
        _audit_log({"audit_id": _gen_audit_id(), "ts": ts, "op": "local_relocate", "source": _SOURCE,
                    "origin": origin, "slug": r["name"], "from_path": r["from"], "to_path": r["to"]})
    path = _undo_path(undo_file)
    _write_undo(path, done, origin, plan["counts"])
    _audit_log({"audit_id": _gen_audit_id(), "ts": ts, "op": "local_relocate_batch", "source": _SOURCE,
                "origin": origin, "counts": plan["counts"], "legacy_index_md_removed": removed,
                "undo_file": str(path)})
    return {"ok": True, "applied": len(done), "legacy_index_md_removed": removed, "undo_file": str(path),
            "tidy_errors": tidy_errors, "sync_error": _sync_memory_index() if sync_index else None}


def undo_local_migration(payload: Dict[str, Any], *, dry_run: bool = False, sync_index: bool = True) -> Dict[str, Any]:
    """照復原憑證倒著做回去：被併、被刪的檔用憑證裡的原文還原，索引列換回舊路徑；最後清掉新位置只剩 _INDEX.md 的資料夾與空資料夾。"""
    if payload.get("kind") != "migrate-local":
        return {"ok": False, "error": "not a migrate-local undo file", "reverted": 0}
    entries = list(payload.get("entries") or [])
    if dry_run:
        problems = []
        for e in entries:
            rel = e.get("rel", "")
            if e["op"] in ("move_card", "move") and not (LOCAL_ATOMS_DIR / rel).exists():
                problems.append(f"{rel}: not found at {LOCAL_ATOMS_REL}/")
            if e["op"] in ("move_card", "move") and (LEGACY_LOCAL_ATOMS_DIR / rel).exists():
                problems.append(f"{rel}: original path occupied")
        return {"ok": not problems, "dry_run": True, "would_revert": len(entries), "problems": problems}
    left, errors = _revert_all(entries)
    _removed, tidy_errors = _tidy_tree(LOCAL_ATOMS_DIR, None, GLOBAL_MEMORY_DIR)
    reverted = len(entries) - len(left)
    _audit_log({"audit_id": _gen_audit_id(), "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "op": "local_relocate_undo", "source": _SOURCE, "reverted": reverted, "failed": len(left)})
    return {"ok": not left, "reverted": reverted, "failed": errors, "tidy_errors": tidy_errors,
            "sync_error": _sync_memory_index() if (sync_index and reverted) else None}


def _public_plan(plan: Dict[str, Any], held_by: Dict[str, List[str]], unreadable: List[str]) -> Dict[str, Any]:
    """預覽輸出：數字、衝突、索引修正與找不到的卡全列；搬移清單太長只列前 50 筆（數字在 counts）。"""
    sessions = sorted({s for v in held_by.values() for s in v})
    return {**{k: v for k, v in plan.items() if k != "items"},
            "items_sample": plan["items"][:50],
            "sessions_holding_legacy_paths": {"sessions": sessions, "paths": len(held_by), "unreadable": unreadable}}


def cmd_migrate_local(args: argparse.Namespace) -> Dict[str, Any]:
    if args.undo:
        payload = json.loads(Path(args.undo).read_text(encoding="utf-8-sig"))
        res = undo_local_migration(payload, dry_run=args.dry_run, sync_index=not args.no_sync_index)
        return {"mode": "UNDO-DRY-RUN" if args.dry_run else "UNDONE", "undo_file": str(args.undo), **res}
    held_by, unreadable = paths_held_by_sessions(CLAUDE_DIR / "workflow")   # 手動跑只報告，不擋（跑它的這個 session 自己也在裡面）
    try:
        plan = plan_local_migration()
    except (OSError, RuntimeError) as e:
        return {"mode": "REFUSED", "ok": False, "error": f"{type(e).__name__}: {e}"}
    if args.dry_run:
        return {"mode": "PREVIEW", "ok": not plan["conflicts"], **_public_plan(plan, held_by, unreadable)}
    res = apply_local_migration(plan, origin="cli", undo_file=args.undo_file, sync_index=not args.no_sync_index)
    mode = "APPLIED" if res.get("ok") else ("REFUSED" if res.get("refused") else "FAILED")
    return {"mode": mode, "counts": plan["counts"], "missing": plan["missing"], **res}


# ─── CLI ──────────────────────────────────────────────────────────────────────


def main() -> int:
    for stream in (sys.stdout, sys.stderr):   # 卡名多是中文：Windows 主控台預設編碼印不出來
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower() != "utf-8":
            stream.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="Move atom between core ⇄ local realm")
    sub = p.add_subparsers(dest="cmd", required=True)

    st = sub.add_parser("set", help="Set an atom's realm (core→local with --domain, or --to-core)")
    st.add_argument("atom", help="atom slug")
    st.add_argument("--domain", default=None, help=t("cli.atom_set_realm.domain_help", domains=sorted(LOCAL_REALM_DOMAINS)))
    st.add_argument("--to-core", action="store_true", help="reverse: move local atom back to core (undo)")
    st.add_argument("--category", default=None,
                    help=t("cli.atom_set_realm.category"))
    st.add_argument("--dry-run", action="store_true")
    st.set_defaults(func=lambda a: set_realm(
        a.atom, domain=a.domain, to_core=a.to_core, dry_run=a.dry_run, category=a.category,
    ))

    mg = sub.add_parser("migrate-local", help=t("cli.atom_set_realm.migrate_help", old=LEGACY_LOCAL_ATOMS_REL, new=LOCAL_ATOMS_REL))
    mg.add_argument("--dry-run", action="store_true", help=t("cli.atom_set_realm.migrate_dry_run"))
    mg.add_argument("--undo", default=None, metavar="UNDO_JSON", help=t("cli.atom_set_realm.migrate_undo"))
    mg.add_argument("--undo-file", default=None, type=Path, help=t("cli.atom_set_realm.migrate_undo_file"))
    mg.add_argument("--no-sync-index", action="store_true", help=t("cli.atom_set_realm.migrate_no_sync_index"))
    mg.set_defaults(func=cmd_migrate_local)

    args = p.parse_args()
    result = args.func(args)
    if result.get("warning"):
        print(t("cli.atom_set_realm.warning_line", warning=result['warning']), file=sys.stderr)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
