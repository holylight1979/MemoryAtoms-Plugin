"""
wg_atoms_realm.py — Realm 自動歸類搬移 sweep：載入分類工具、讀卡摘要供判定、掃文件引用、SessionEnd 把卡搬到該去的範疇。

誰叫它：SessionEnd 經 wg_atoms 呼叫。
純搬移自 wg_atoms.py，函式本體未改；hooks/wg_atoms.py 仍 re-export 這裡的名稱。
"""

import json
from typing import Any, Dict, List, Optional
from wg_core import CLAUDE_DIR, CODE_DIR, MEMORY_DIR, REALM_AUTOMOVE_MARKER, _atom_debug_error
from pathlib import Path
from datetime import datetime, timezone
from wg_atoms_compat import (
    ENTRY_BOUND_STAYS_CORE,
    LOCAL_REALM_DEFAULT_DOMAIN,
    append_learned_terms,
    classify_realm,
    core_protected_source,
    entry_bound_ids,
    enumerate_local_paths,
    is_in_failures_path,
    is_local_realm_path,
    load_atom_index_json,
    load_learned_lexicon,
)
from wg_atoms_forget import _trigger_sync_memory_index
from wg_atoms_index import scope_from_rel_path


# ─── V5+ Realm 維度：SessionEnd 自動歸類搬移 sweep ──────────────────────────


def _load_tool_module(filename: str, mod_name: str):
    """以 spec_from_file_location 載 tools/ 下單檔模組（self-sufficient sys.path）。失敗→None。"""
    try:
        import importlib.util
        p = CODE_DIR / "tools" / filename
        spec = importlib.util.spec_from_file_location(mod_name, p)
        if spec is None or spec.loader is None:
            return None
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    except Exception as e:
        _atom_debug_error(f"realm:load_{mod_name}", e)
        return None


def _read_atom_excerpt(rel_path: str, limit: int = 800) -> str:
    """讀 atom 內文摘要供 LLM 判定（utf-8-sig 容 BOM）。失敗→''。"""
    try:
        return (CLAUDE_DIR / rel_path).read_text(encoding="utf-8-sig")[:limit]
    except OSError:
        return ""


def _is_unconfirmed_autocapture(entry: Dict[str, Any]) -> bool:
    """index entry 是否為『未確認的 auto-capture 萃取碎片』→ drift sweep defer（不搬、不喚 LLM 學詞）。

    auto-captured 碎片本是未經人確認的 [臨] 萃取，sweep 卻當穩定 core atom
    處理 → LLM 對其吐專案/標籤詞污染學習詞庫（外部專案知識被搬進根層 _atoms/、碎片被塞進
    名為 "auto-capture" 的葉夾即此）。整體 defer 斷源頭，待人工確認 / 晉升（[臨]→[觀]）後才正常
    sweep 歸檔。

    判定（index-only 優先，零 file I/O）：triggers 含 'auto-capture'（extract-worker 預設標籤，
    涵蓋現存全部污染）。次判（frontmatter，非熱路徑）：Author==auto-captured 且 Confidence==[臨]
    ——catch『domain_tags 已填、trigger 非預設』但仍未確認的碎片；晉升後 Confidence 變 → 不再 defer。
    """
    for t in (entry.get("triggers") or []):
        if "auto-capture" in str(t).lower():
            return True
    author = conf = ""
    for line in _read_atom_excerpt(entry.get("path") or "", limit=400).splitlines():
        s = line.strip()
        if s.startswith("- Author:"):
            author = s.split(":", 1)[1].strip().lower()
        elif s.startswith("- Confidence:"):
            conf = s.split(":", 1)[1].strip()
        if author and conf:
            break
    return author == "auto-captured" and conf == "[臨]"


def _autocapture_unconfirmed_from_text(text: str) -> bool:
    """body 全文判『未確認 auto-capture 碎片』（晉升掃描面用，零額外 file I/O）。

    規則與 _is_unconfirmed_autocapture **同一條**，只是輸入適配器不同（index entry vs 已載入
    body 全文）：① `- Trigger:` 行含 'auto-capture'（index triggers 即由此行建，已實證 byte-mirror
    → 等價）；② Author==auto-captured 且 Confidence==[臨]。兩者皆 catch 未經人確認的 [臨] 萃取碎片。
    改動判定規則時兩函式須同步（adapter 並存、規則單源）。
    """
    trig = author = conf = ""
    for line in text.splitlines():
        s = line.strip()
        if not trig and s.startswith("- Trigger:"):
            trig = s.split(":", 1)[1].lower()
        elif not author and s.startswith("- Author:"):
            author = s.split(":", 1)[1].strip().lower()
        elif not conf and s.startswith("- Confidence:"):
            conf = s.split(":", 1)[1].strip()
        if trig and author and conf:
            break
    if "auto-capture" in trig:
        return True
    return author == "auto-captured" and conf == "[臨]"


def _is_atom_physical_rel(rel: str) -> bool:
    """rel（相對 ~/.claude、POSIX）落在 atom 物理區（失敗家族新舊址 / _AIDocs/_atoms/）⇒ True。"""
    if is_in_failures_path is not None and is_local_realm_path is not None:
        return is_in_failures_path(rel) or is_local_realm_path(rel)
    return (rel.startswith("memory/Failures/") or rel.startswith("_AIDocs/Failures/")
            or rel.startswith("_AIDocs/_atoms/"))


def _scan_doc_refs(moved: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """搬移後掃**人面向說明文件**是否仍含舊 path/檔名引用（移檔非建檔特有；user 補充）。

    回 {slug: [需同步的 rel 文件...]}。只掃 _AIDocs/（排除 atom 物理區：舊址 Failures/ 與 _atoms/，
    那裡的 slug 引用是 atom-atom Related、搬 path 不斷）＋根層 README/TECH 與 TECH/ 細節檔。advisory only。
    """
    docs: List[Path] = []
    aidocs = CLAUDE_DIR / "_AIDocs"
    if aidocs.is_dir():
        for p in aidocs.rglob("*.md"):
            rel = p.relative_to(CLAUDE_DIR).as_posix()
            if _is_atom_physical_rel(rel):
                continue
            docs.append(p)
    for fn in ("README.md", "TECH.md"):
        p = CLAUDE_DIR / fn
        if p.exists():
            docs.append(p)
    docs.extend(sorted((CLAUDE_DIR / "TECH").glob("*.md")))
    cache = {}
    for p in docs:
        try:
            cache[p] = p.read_text(encoding="utf-8")
        except OSError:
            continue
    refs: Dict[str, List[str]] = {}
    for m in moved:
        slug, frm = m.get("slug", ""), m.get("from", "")
        fname = frm.rsplit("/", 1)[-1] if frm else f"{slug}.md"
        hits = sorted({
            p.relative_to(CLAUDE_DIR).as_posix()
            for p, txt in cache.items() if (frm and frm in txt) or fname in txt
        })
        if hits:
            refs[slug] = hits
    return refs


def _sweep_realm_auto_migrate(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """SessionEnd：掃全域 core atom，自動歸 local（drift 補捉）。非熱路徑。

    兩段判定：① 詞庫（deterministic，含 py-only learned 補 recall）命中 local → 搬；
    ② 詞庫 miss 的「unknown core」（非 protected）→ 喚 LLM（計畫 Fail-safe 表）：
      error（基礎設施失敗）→ defer 留原地（**防 Ollama 離線把全部掃進 Else**）；
      core → 留；local≥門檻 → 搬 canon domain + 學詞；unsure/低信心 → Else。
    安全網：核心保護硬擋（protected）永不喚 LLM、永不搬；ENTRY_BOUND_STAYS_CORE（綁 Entry 的卡）
      本來要搬或要喚 LLM 時改成不搬、不喚，留帳 atom_io_audit.jsonl op=realm_hold；set_realm 原子搬（含 .access.json）/
      可 undo（--to-core）；max_per_session 限額；搬移寫 marker（含 via + doc-ref）不靜默。
    回 list of {slug, domain, from, to, via}（空 = 無搬移）。
    """
    if (classify_realm is None or is_local_realm_path is None or load_atom_index_json is None
            or entry_bound_ids is None or core_protected_source() != "json"):   # 詞表沒載入：判不出保護名，這輪不搬（開場紅字已講）
        return []
    realm_cfg = config.get("realm", {})
    if not realm_cfg.get("auto_migrate", True):
        return []

    llm_cfg = realm_cfg.get("llm_fallback", {})
    llm_enabled = bool(llm_cfg.get("enabled", False))
    max_llm = int(llm_cfg.get("max_per_session", 5))
    min_conf = float(llm_cfg.get("min_confidence", 0.7))
    learned = load_learned_lexicon() if load_learned_lexicon else {}
    default_dom = LOCAL_REALM_DEFAULT_DOMAIN

    moved: List[Dict[str, Any]] = []
    learned_add: Dict[str, str] = {}
    llm_calls = 0
    try:
        mod = _load_tool_module("atom-set-realm.py", "atom_set_realm")
        if mod is None:
            return []
        from lib.atom_io import _audit_log, _gen_audit_id  # atom-set-realm 載入時已把程式碼根放進 sys.path
        llm_mod = _load_tool_module("realm_llm_classify.py", "realm_llm_classify") if llm_enabled else None
        existing_paths = list(enumerate_local_paths(MEMORY_DIR)) if enumerate_local_paths else []

        data = load_atom_index_json(MEMORY_DIR)
        for a in data.get("atoms", []):
            name = a.get("name", "")
            path = a.get("path", "")
            if not name or is_local_realm_path(path):
                continue  # 已 local，跳過（idempotent）
            if scope_from_rel_path(path, "global").startswith("personal:"):
                continue  # 本人跨專案 personal：只給本人，不進 realm 搬移
            if _is_unconfirmed_autocapture(a):
                continue  # P2: 未確認 auto-capture 碎片 → defer（不搬、不喚 LLM 學詞，斷詞庫污染源）
            rc = classify_realm(name, a.get("triggers", []), extra_lexicon=learned or None)
            if rc.get("protected"):
                continue  # 核心保護硬擋：永不喚 LLM、永不搬
            lex_local = rc.get("realm") == "local"
            if not lex_local and (llm_mod is None or llm_calls >= max_llm):
                continue  # LLM 未啟用 / 額度用罄 → 留 core（defer）
            entry_ids = entry_bound_ids(a, CLAUDE_DIR / path)
            if entry_ids:  # ENTRY_BOUND_STAYS_CORE：本來要搬或要喚 LLM，改成不搬、不喚，留帳
                _audit_log({
                    "audit_id": _gen_audit_id(),
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),  # 同 realm_move 帳用 UTC
                    "op": "realm_hold", "source": "hook:realm-sweep", "rule": ENTRY_BOUND_STAYS_CORE,
                    "slug": name, "entry": entry_ids, "path": path,
                    "would": f"lex:{rc.get('domain')}" if lex_local else "LLM",
                })
                continue

            target_dom: Optional[str] = None
            via: Optional[str] = None
            if lex_local:
                target_dom, via = rc.get("domain"), "lex"
            else:
                llm_calls += 1
                lr = llm_mod.llm_classify_realm(
                    name, a.get("triggers", []), _read_atom_excerpt(path), existing_paths, config)
                realm = lr.get("realm")
                if realm in ("error", "core"):
                    continue  # 基礎設施失敗→defer / LLM 確信核心→留
                if realm == "local" and lr.get("confidence", 0.0) >= min_conf:
                    target_dom, via = lr.get("domain_path") or default_dom, "LLM"
                    for t in lr.get("terms", []):
                        learned_add[t] = target_dom
                    if target_dom:  # 新分支同 session 後續可複用
                        existing_paths = sorted(set(existing_paths) | {target_dom})
                else:  # unsure / 低信心 local → catch-all
                    target_dom, via = default_dom, "Else"

            if not target_dom:
                continue
            res = mod.set_realm(name, domain=target_dom)
            if res.get("ok") and not res.get("noop"):
                moved.append({
                    "slug": name, "domain": target_dom, "via": via,
                    "from": res.get("from"), "to": res.get("to"),
                })
    except Exception as e:
        _atom_debug_error("realm:auto_sweep", e)

    # 收尾：學詞回寫 → marker（含 via + doc-ref）→ 補觸發 catalog 重產
    if learned_add and append_learned_terms:
        try:
            append_learned_terms(learned_add)
        except Exception as e:
            _atom_debug_error("realm:learned_append", e)

    if moved:
        doc_refs = {}
        try:
            doc_refs = _scan_doc_refs(moved)
        except Exception as e:
            _atom_debug_error("realm:doc_ref_scan", e)
        try:
            REALM_AUTOMOVE_MARKER.parent.mkdir(parents=True, exist_ok=True)
            existing: List[Dict[str, Any]] = []
            if REALM_AUTOMOVE_MARKER.exists():
                try:
                    prev = json.loads(REALM_AUTOMOVE_MARKER.read_text(encoding="utf-8"))
                    if isinstance(prev, list):
                        existing = prev
                except (OSError, json.JSONDecodeError):
                    existing = []
            payload = list(moved)
            if doc_refs:  # 附在首筆，SessionStart 統一呈現
                payload[0] = {**payload[0], "doc_refs": doc_refs}
            existing.extend(payload)
            REALM_AUTOMOVE_MARKER.write_text(
                json.dumps(existing, ensure_ascii=False), encoding="utf-8", newline="\n")
        except OSError as e:
            _atom_debug_error("realm:automove_marker", e)
        _trigger_sync_memory_index()
    return moved
