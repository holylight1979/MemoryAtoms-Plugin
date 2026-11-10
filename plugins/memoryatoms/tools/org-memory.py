#!/usr/bin/env python3
"""org-memory.py — 組織層記憶（org）初始化與工具卡掃描。

做什麼：把一個已 checkout 的組織記憶 repo 根佈成「所有專案都看得到、都能寫」的記憶層：
  <root>/.claude/memory/{MEMORY.md（含 atom-catalog 區塊）, _atom_index.json, shared/, shared/_taxonomy.json}
  <root>/.claude/project-tree.json {"standalone": true}（專案往上尋根到此為止）
  種一顆工具卡 shared/工具/org-memory.md（索引非空，sync-memory-index --check 才能過）
  ~/.claude/workflow/org-memory.local.json（本機專屬、不進版控）enabled=true, roots=[{"id":"org","root":<root>}]
  ~/.claude/memory/project-registry.json 登錄條目（向量 indexer 據此建層 shared:<slug>）
全部冪等：已存在的檔不覆寫。
--scan-tools：把 skills/_skill_index.json、mcp-servers.template.json 的每個工具登記成 org shared/工具/ 一張卡
  （--project <root> 時另掃 <root>/.claude/tools/*.py 落該專案 shared/工具/）。只建缺的卡，既有卡一律不動；
  卡的進入點（Depends: path:）消失 → 把 Status 改 deprecated，不刪卡。
  卡名：skill → skill-<name>、MCP server → mcp-<name>（一眼看出種類、避開索引檔 MEMORY.md 撞名）、
  專案 tools/*.py → 檔名去副檔名（與 --init 種的 org-memory 一致）。
--join：新機器一步接上——根目錄不存在就從 config org_memory.repo_url clone，再跑 --init。
--status：對帳——組織層根、是否就緒、atom 與工具卡數、git 同步狀態、目前身份與職能、裁決名單。
怎麼跑：
  python <記憶系統>/tools/org-memory.py --join [<本機路徑>]      # 省略路徑：本機已記的根 → config org_memory.default_root
  python <記憶系統>/tools/org-memory.py --decline                 # 這台先不接；啟動時不再詢問
  python <記憶系統>/tools/org-memory.py --status
  python <記憶系統>/tools/org-memory.py --init <組織記憶 repo 根>
  python <記憶系統>/tools/org-memory.py --scan-tools [--project <專案根>] [--owner <AD 帳號>] [--dry-run]
  完整參數以 --help 為準。
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

CODE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE_DIR))
sys.path.insert(0, str(CODE_DIR / "hooks"))

import wg_core  # noqa: E402
from edition import layered_config  # noqa: E402  lib/（wg_core 已放進 sys.path）：插件版出廠設定墊底
from wg_platform import no_window_flags, paste_command  # noqa: E402
from wg_roles import get_current_user  # noqa: E402
from lib import atom_access  # noqa: E402
from lib.i18n import t  # noqa: E402
from lib.atom_io import (  # noqa: E402
    card_body_lines, edit_metadata, locate_atom, narrative_date_error, write_index, write_raw,
)
from lib.atom_spec import (  # noqa: E402
    TRIGGER_MIN, build_atom_content, classify_depends_path, depends_base_for, parse_depends, portable_depends_path,
    slugify, validate_atom_content,
)
from lib.atom_index_json import load_atom_index_json, upsert_atom  # noqa: E402

CONFIG_PATH = wg_core.CONFIG_PATH
SYNC_INDEX = CODE_DIR / "tools" / "sync-memory-index.py"
TAXONOMY = {
    "domains": {
        "工具": {
            "desc": "工具卡：Author=負責人、Source=進入點、Status=production|deprecated、Depends=path:進入點",
        },
    },
}
SEED_NAME = "org-memory"
ACCESS_IGNORE = "**/*.access.json"
SEED_REL = f"memory/shared/工具/{SEED_NAME}.md"
SEED_TRIGGERS = ["org-memory", "組織層記憶", "org 初始化", "scope=org"]

# --scan-tools 來源（測試 monkeypatch 這三個）
SKILL_INDEX = CODE_DIR / "skills" / "_skill_index.json"
SKILLS_DIR = CODE_DIR / "skills"
MCP_TEMPLATE = CODE_DIR / "mcp-servers.template.json"
TOOL_DOMAIN = "工具"
# audit source：VALID_SOURCES 內最貼切者——把外部 SoT（skills 索引／MCP 樣板／tools 目錄）同步進記憶
SCAN_SOURCE = "tool:sync-memory-index"
_STATUS_RE = re.compile(r"^- Status:\s*(.*)$", re.MULTILINE)
_DEPENDS_RE = re.compile(r"^- Depends:\s*(.+)$", re.MULTILINE)


def _write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _sync_catalog(mem: Path) -> str:
    """MEMORY.md atom-catalog 區塊交給 sync-memory-index 專案模式（marker upsert）。"""
    r = subprocess.run(
        [sys.executable, str(SYNC_INDEX), "--write", "--memory-dir", str(mem)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=no_window_flags(),
    )
    if r.returncode != 0:
        raise RuntimeError(t("cli.org_memory.sync_failed", code=r.returncode, error=r.stderr.strip()))
    return t("cli.org_memory.catalog_synced")


def init_tree(root: Path, user: str) -> List[str]:
    """佈 <root>/.claude 記憶樹（純檔案系統，不碰 ~/.claude）。回逐項訊息。"""
    mem = root / ".claude" / "memory"
    done: List[str] = []

    decl = root / ".claude" / "project-tree.json"
    if not decl.exists():
        _write_lf(decl, json.dumps({"standalone": True}, ensure_ascii=False, indent=2) + "\n")
        done.append(t("cli.org_memory.made", path=decl))

    # access sidecar 是各機自己的使用遙測（vcs-sync 也排除它）；進版控只會讓 repo 永遠髒、多機互撞
    ignore = root / ".gitignore"
    ignore_text = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
    if ACCESS_IGNORE not in ignore_text.splitlines():
        _write_lf(ignore, ignore_text + ("" if not ignore_text or ignore_text.endswith(chr(10)) else chr(10))
                  + ACCESS_IGNORE + chr(10))
        done.append(t("cli.org_memory.wrote_ignore", path=ignore, pattern=ACCESS_IGNORE))

    tax = mem / "shared" / "_taxonomy.json"
    if not tax.exists():
        _write_lf(tax, json.dumps(TAXONOMY, ensure_ascii=False, indent=2) + "\n")
        done.append(t("cli.org_memory.made_taxonomy", path=tax))

    seed = mem / "shared" / "工具" / f"{SEED_NAME}.md"
    if not seed.exists():
        entry = str(Path(__file__).resolve())
        content = build_atom_content(
            title=SEED_NAME, scope="shared", confidence="[臨]", triggers=SEED_TRIGGERS,
            knowledge=[
                "[臨] 組織層記憶（org）＝這個 repo 的 .claude/memory；所有專案的 session 都看得到，"
                "寫入用 atom_write scope=org（落 shared/<Lv1>/，免 project_cwd）",
                "[臨] 工具卡四欄：Author=負責人、Source=進入點、Status=production|deprecated、"
                "Depends=path:進入點（進入點消失健檢自動標 stale）",
            ],
            actions=[
                "新機初始化：記憶系統的 tools/org-memory.py --init <組織記憶 repo 的本機 checkout 路徑>",
                "查組織知識：記憶系統的 tools/memory-search.py \"關鍵字\"",
            ],
            author=user, status="production", provenance=entry, depends=[f"path:{portable_depends_path(Path(entry))}"],
        )
        err = validate_atom_content(content)
        if err:
            raise RuntimeError(t("cli.org_memory.seed_invalid", error=err))
        _write_lf(seed, content)
        atom_access.init_access(seed, source=SCAN_SOURCE)   # 與掃描器建卡一致：卡＋sidecar 一起落
        upsert_atom(mem, SEED_NAME, SEED_REL, SEED_TRIGGERS, scope="shared")
        done.append(t("cli.org_memory.seeded", path=seed))

    memory_md = mem / "MEMORY.md"
    if not memory_md.exists():
        _write_lf(memory_md, "# Atom Index — Org\n\n> 組織層記憶：所有專案看得到；寫入 `atom_write scope=org`。\n")
        done.append(t("cli.org_memory.made", path=memory_md))

    done.append(_sync_catalog(mem))
    return done


def register_local(root: Path) -> str:
    """本機狀態檔（不進版控）← enabled=true, roots=[{id:org, root}]；共用 config.json 不動。"""
    wg_core.save_org_local(enabled=True, roots=[{"id": "org", "root": str(root)}], declined=False)
    return f"{wg_core.org_local_path().name} enabled=true roots=[{root}]"


def register_in_registry(root: Path) -> str:
    """project-registry.json 直接寫條目（不走 register_project：它要 cwd 已是專案）。"""
    slug = wg_core.cwd_to_project_slug(str(root))
    reg = wg_core._load_registry()
    entry = reg.setdefault("projects", {}).setdefault(slug, {})
    entry["root"] = str(root)
    entry["last_seen"] = wg_core._today()
    wg_core._save_registry(reg)
    return t("cli.org_memory.registered", slug=slug)


def cmd_init(root_arg: str) -> int:
    root = Path(root_arg).expanduser().resolve()
    if not root.is_dir():
        print(t("cli.org_memory.root_missing", root=root), file=sys.stderr)
        return 2
    if wg_core.find_vcs_root(root) is None:
        print(t("cli.org_memory.no_vcs", root=root), file=sys.stderr)
    user = get_current_user()
    had_index = (root / ".claude" / "memory" / "_atom_index.json").is_file()   # 現成組織層（--join clone 下來）還是這台新佈的
    msgs = init_tree(root, user)
    msgs.append(register_local(root))
    msgs.append(register_in_registry(root))
    print(t("cli.org_memory.done"))
    for m in msgs:
        print(f"  - {m}")
    print(t("cli.org_memory.next_steps"))
    if had_index:
        print(t("cli.org_memory.step1_existing"))
    else:
        print(t("cli.org_memory.step1_commit", path=root / '.claude'))
    print(t("cli.org_memory.step2"))
    print(t("cli.org_memory.step3", cmd=paste_command(CODE_DIR / "tools" / "memory-search.py")))
    return 0


# ─── --scan-tools：來源 → 卡片規格 ────────────────────────────────────────────

def _card_triggers(name: str, kind: str) -> List[str]:
    """工具卡的觸發詞只放「卡名」與「名稱＋種類」片語，不放裸名、種類單字與說明關鍵字。
    工具名多是 memory／handoff／continue 這類日常字，種類字（skill／mcp）更是句句會出現——
    放了每句話都把整批工具卡注入、吃光注入預算。用工具名查仍找得到：memory_search 的 BM25 以卡名與觸發詞斷詞。"""
    if kind == TOOL_DOMAIN:
        return [f"{name}.py", f"{name} 工具", f"工具卡 {name}"]
    return [f"{kind}-{name}", f"{name} {kind}", f"工具卡 {name}"]


def _card(name: str, kind: str, desc: str, *, entry: str, has_path: bool, run: str, dep: str = "") -> Dict[str, object]:
    """卡名＝kind 前綴＋名稱（kind 為「工具」的專案腳本不加前綴）。dep＝寫進 Depends 的可攜路徑（`~/.claude/<rel>`／專案相對），
    沒給就用 entry；卡片跨機共享，作者機的絕對路徑別台機器驗不到。"""
    card_name = name if kind == TOOL_DOMAIN else f"{kind}-{name}"
    triggers = _card_triggers(name, kind)
    assert len(triggers) >= TRIGGER_MIN
    return {
        "name": card_name, "entry": entry, "dep": dep or entry, "has_path": has_path, "triggers": triggers,
        "desc": desc or "（來源未附說明）", "run": run, "kind": kind,
    }


def _load_json(path: Path, label: str) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(t("cli.org_memory.load_skipped", label=label, path=path, error=e), file=sys.stderr)
        return None


def collect_skill_cards() -> List[Dict[str, object]]:
    """skills/_skill_index.json 每個 skill 一張卡 skill-<name>；進入點＝skills/<dir>/SKILL.md。"""
    data = _load_json(SKILL_INDEX, "skills")
    if not data:
        return []
    cards = []
    for s in data.get("skills", []):
        name = s.get("name")
        if not name:
            continue
        entry = (SKILLS_DIR / (s.get("dir") or name) / "SKILL.md").resolve().as_posix()
        cards.append(_card(name, "skill", s.get("description", ""), entry=entry, has_path=True,
                           run=f"Claude Code 內呼叫 /{name}", dep=portable_depends_path(Path(entry))))
    return cards


def collect_mcp_cards() -> List[Dict[str, object]]:
    """mcp-servers.template.json servers 每張卡 mcp-<name>；進入點＝entry_absolute 展開，否則 npm 套件名（無路徑 → 無 Depends）。"""
    data = _load_json(MCP_TEMPLATE, "mcp")
    if not data:
        return []
    cards = []
    for name, d in (data.get("servers") or {}).items():
        abs_entry = d.get("entry_absolute")
        if abs_entry:
            entry = Path(abs_entry.replace("{claude_dir}", str(CODE_DIR))).resolve().as_posix()
        else:
            entry = d.get("npm_package") or name
        cards.append(_card(name, "mcp", d.get("description", ""), entry=entry, has_path=bool(abs_entry),
                           run=f"MCP server「{name}」由 hooks/ensure-mcp.py 依 mcp-servers.template.json 註冊，Claude Code 啟動即載入",
                           dep=portable_depends_path(Path(entry)) if abs_entry else ""))
    return cards


def _first_doc_paragraph(py: Path) -> str:
    try:
        doc = ast.get_docstring(ast.parse(py.read_text(encoding="utf-8-sig")))
    except (OSError, SyntaxError, ValueError):
        doc = None
    if not doc:
        return ""
    return " ".join(line.strip() for line in doc.strip().split("\n\n")[0].splitlines())


def collect_project_cards(root: Path) -> List[Dict[str, object]]:
    """<root>/.claude/tools/*.py 每檔一張卡；說明＝檔頭 docstring 第一段。"""
    cards = []
    for py in sorted((root / ".claude" / "tools").glob("*.py")):
        entry = py.resolve().as_posix()
        cards.append(_card(py.stem, TOOL_DOMAIN, _first_doc_paragraph(py), entry=entry, has_path=True,
                           run=f"python {entry} --help", dep=portable_depends_path(py, project_root=root)))
    return cards


# ─── --scan-tools：寫卡（create-if-absent）與退役 ─────────────────────────────

def write_card(card: Dict[str, object], *, project_cwd: Path, owner: str, dry_run: bool) -> str:
    """缺卡才建；回一行訊息，「失敗」開頭表示該卡沒寫成（呼叫端據此 exit 1）。"""
    name = str(card["name"])
    triggers = list(card["triggers"])  # type: ignore[arg-type]
    loc = locate_atom(name, "shared", project_cwd=str(project_cwd), domain=TOOL_DOMAIN, mode="create",
                      allow_new_category=True, triggers=triggers)
    if not loc.ok and "Slug collision" in (loc.error or ""):
        # funnel 的撞名保護（例：專案 tools/memory.py 撞索引檔 MEMORY.md）：不是寫入失敗，改名要人拍板
        return t("cli.org_memory.card_skipped", name=name, error=loc.error.splitlines()[0])
    if not loc.ok:
        return t("cli.org_memory.card_failed", name=name, error=loc.error)
    slug = loc.extra["slug"]
    if loc.path is not None:
        return t("cli.org_memory.card_exists", name=slug)
    entry = str(card["entry"])
    content = build_atom_content(
        title=name, scope="shared", confidence="[臨]", triggers=triggers,
        knowledge=[f"[臨] {card['desc']}"], actions=[str(card["run"])],
        author=owner, status="production", provenance=entry,
        depends=[f"path:{card.get('dep') or entry}"] if card["has_path"] else None,
    )
    # 工具說明整句進知識行：帶敘事日期就不建卡（寫入閘 KNOWLEDGE_LINE_NO_NARRATIVE_DATE，與 write_atom 同一份判式）
    err = validate_atom_content(content) or narrative_date_error(card_body_lines(content))
    if err:
        return t("cli.org_memory.card_failed", name=slug, error=err)
    target = Path(loc.extra["target_dir"]) / f"{slug}.md"
    if dry_run:
        return t("cli.org_memory.dry_create", path=target)
    res = write_raw(target, content, source=SCAN_SOURCE, op="tool-card")
    if not res.ok:
        return t("cli.org_memory.card_failed", name=slug, error=res.error)
    idx = write_index(Path(loc.extra["index_dir"]), slug, loc.extra["create_rel_path"], triggers,
                      SCAN_SOURCE, scope="shared")
    if not idx.ok:
        return t("cli.org_memory.card_failed_index", name=slug, error=idx.error)
    atom_access.init_access(target, source=SCAN_SOURCE)
    return t("cli.org_memory.created", path=target)


def deprecate_missing(mem_dir: Path, *, dry_run: bool) -> List[str]:
    """shared/工具/ 下 path 型 Depends 任一「本機該在卻不在」的卡 → Status: deprecated（不刪卡；已 deprecated 不重寫）。
    別台機器的絕對路徑（classify_depends_path 判 foreign）本機驗不到，不退役：組織層的卡是作者機寫的，在別台機器掃不能把它們全退役。"""
    msgs: List[str] = []
    base = depends_base_for(mem_dir)
    for e in load_atom_index_json(mem_dir).get("atoms", []):
        rel = str(e.get("path") or "").replace("\\", "/")
        if f"/{TOOL_DOMAIN}/" not in rel:
            continue
        path = mem_dir.parent / rel  # index path 以 <root>/.claude 為根
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8-sig")
        dm = _DEPENDS_RE.search(text)
        if not dm:
            continue
        missing = [d["value"] for d in parse_depends(dm.group(1))
                   if d["type"] == "path" and d["value"] and classify_depends_path(d["value"], base=base)[0] == "missing"]
        if not missing:
            continue
        sm = _STATUS_RE.search(text)
        if sm and sm.group(1).strip() == "deprecated":
            continue
        if not sm:
            msgs.append(t("cli.org_memory.no_status", name=e['name'], entry=missing[0]))
            continue
        if dry_run:
            msgs.append(t("cli.org_memory.dry_retire", name=e['name'], entry=missing[0]))
            continue
        res = write_raw(path, _STATUS_RE.sub("- Status: deprecated", text, count=1),
                        source=SCAN_SOURCE, op="meta-edit")
        msgs.append(t("cli.org_memory.retired", name=e['name'], entry=missing[0]) if res.ok
                    else t("cli.org_memory.card_failed", name=e['name'], error=res.error))
    return msgs


def refresh_legacy_triggers(mem_dir: Path, cards: List[Dict[str, object]], *, dry_run: bool) -> List[str]:
    """舊世代工具卡的觸發詞含種類單字（skill／mcp／工具）→ 換成現行規則（只動 Trigger 行與索引）。
    判準是「索引裡的觸發詞含種類單字」：那只可能是掃描器早期產生的；人工改過觸發詞的卡不會命中、不動。"""
    by_name = {str(e.get("name")): e for e in load_atom_index_json(mem_dir).get("atoms", [])}
    msgs: List[str] = []
    for card in cards:
        e = by_name.get(slugify(str(card["name"])))   # 索引名是 slug（小寫），卡名可能含大寫
        if not e or str(card["kind"]) not in (e.get("triggers") or []):
            continue
        if dry_run:
            msgs.append(t("cli.org_memory.dry_triggers", name=card['name']))
            continue
        path = mem_dir.parent / str(e.get("path") or "").replace(chr(92), "/")
        res = edit_metadata(path, triggers=list(card["triggers"]), source=SCAN_SOURCE)  # type: ignore[arg-type]
        msgs.append(t("cli.org_memory.triggers_updated", name=card['name']) if res.ok
                    else t("cli.org_memory.triggers_failed", name=card['name'], error=res.error))
    return msgs


def cmd_scan_tools(project_arg: Optional[str], owner: Optional[str], dry_run: bool) -> int:
    owner = owner or get_current_user()
    jobs = []  # (標籤, 落點 root, 卡片清單)
    org_root = wg_core.org_memory_root()
    if org_root is not None and not (org_root / ".claude" / "memory").is_dir():
        print(t("cli.org_memory.org_not_init", root=org_root), file=sys.stderr)
        org_root = None
    if org_root is not None:
        jobs.append(("org", org_root, collect_skill_cards() + collect_mcp_cards()))
    if project_arg:
        project = Path(project_arg).expanduser().resolve()
        if not (project / ".claude" / "memory").is_dir():
            print(t("cli.org_memory.not_project_root", project=project), file=sys.stderr)
            return 2
        jobs.append(("project", project, collect_project_cards(project)))
    if not jobs:
        print(t("cli.org_memory.nothing_to_scan"), file=sys.stderr)
        return 2

    failed = 0
    for label, root, cards in jobs:
        tag = t("cli.org_memory.dry_run_tag") if dry_run else ""
        print(t("cli.org_memory.scan_header", label=label, root=root, count=len(cards), owner=owner, tag=tag))
        msgs = [write_card(c, project_cwd=root, owner=owner, dry_run=dry_run) for c in cards]
        msgs += deprecate_missing(root / ".claude" / "memory", dry_run=dry_run)
        msgs += refresh_legacy_triggers(root / ".claude" / "memory", cards, dry_run=dry_run)
        for m in msgs:
            print(f"  - {m}")
            if m.startswith(t("cli.org_memory.p_skip")):
                print(f"[org-memory] {m}", file=sys.stderr)
        failed += sum(1 for m in msgs if m.startswith(t("cli.org_memory.p_fail")))
        changed = any(m.startswith((t("cli.org_memory.p_created"), t("cli.org_memory.p_retired"), t("cli.org_memory.p_trig"))) for m in msgs)
        if changed:
            try:
                print(f"  - {_sync_catalog(root / '.claude' / 'memory')}")
            except RuntimeError as e:
                print(t("cli.org_memory.catalog_failed", error=e))
                failed += 1
    if failed:
        print(t("cli.org_memory.n_failed", count=failed), file=sys.stderr)
        return 1
    return 0


def _org_cfg() -> Dict[str, object]:
    """共用 config 的 org_memory（repo_url／default_root）被本機狀態檔（enabled／roots）蓋過後的結果。"""
    cfg = layered_config(CONFIG_PATH)
    org = cfg.get("org_memory")
    return {**(org if isinstance(org, dict) else {}), **wg_core.load_org_local()}


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120,
                          creationflags=no_window_flags())


def cmd_join(root_arg: Optional[str]) -> int:
    """新機器一步接上：根目錄不存在 → 從 config org_memory.repo_url clone；之後跑 --init（冪等）。
    路徑順位：參數 → 本機已記的根 → 共用 config org_memory.default_root。"""
    org = _org_cfg()
    roots = org.get("roots") or []
    cfg_root = roots[0].get("root") if roots and isinstance(roots[0], dict) else None  # type: ignore[index,union-attr]
    target = root_arg or cfg_root or org.get("default_root")
    if not target:
        print(t("cli.org_memory.no_local_path"), file=sys.stderr)
        return 2
    root = Path(str(target)).expanduser()
    if not root.exists():
        url = str(org.get("repo_url") or "")
        if not url:
            print(t("cli.org_memory.cannot_clone", root=root), file=sys.stderr)
            return 2
        print(f"[org-memory] clone {url} → {root}")
        r = subprocess.run(["git", "clone", url, str(root)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300,
                           creationflags=no_window_flags())
        if r.returncode != 0:
            print(t("cli.org_memory.clone_failed", error=(r.stderr or r.stdout).strip()[-300:]), file=sys.stderr)
            return 1
    return cmd_init(str(root))


def cmd_decline() -> int:
    """使用者答「先不接」：記在本機狀態檔，SessionStart 不再問；之後 --join 仍可接上。"""
    wg_core.save_org_local(declined=True)
    print(t("cli.org_memory.declined", path=wg_core.org_local_path()))
    return 0


def cmd_status() -> int:
    """對帳：組織層接上沒、裡面有什麼、同步狀態、我是誰／什麼職能／誰能裁決。印 JSON。"""
    from wg_roles import load_management_roster, load_user_role
    org = _org_cfg()
    root = wg_core.org_memory_root()
    out: Dict[str, object] = {"enabled": bool(org.get("enabled")), "repo_url": org.get("repo_url") or None,
                              "root": str(root) if root else None, "ready": False,
                              "declined": bool(org.get("declined"))}
    if root is not None and (root / ".claude" / "memory").is_dir():
        mem = root / ".claude" / "memory"
        atoms = load_atom_index_json(mem).get("atoms", [])
        out["ready"] = True
        out["atoms"] = len(atoms)
        out["tool_cards"] = sum(
            1 for e in atoms if f"/{TOOL_DOMAIN}/" in str(e.get("path") or "").replace(chr(92), "/"))
        st = _git(root, "status", "--porcelain", "--", ".claude/memory")
        ab = _git(root, "rev-list", "--left-right", "--count", "@{u}...HEAD")
        out["uncommitted"] = (len([ln for ln in (st.stdout or "").splitlines() if ln.strip()])
                              if st.returncode == 0 else None)
        if ab.returncode == 0 and len(ab.stdout.split()) == 2:
            out["behind"], out["ahead"] = (int(x) for x in ab.stdout.split())
    user = get_current_user()
    role = load_user_role(str(Path.cwd()), user)
    out["user"] = user
    out["roles"] = role.get("roles")
    out["roles_source"] = role.get("source")
    out["deciders"] = load_management_roster(str(Path.cwd())) or t("cli.org_memory.all_members")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=t("cli.org_memory.description"))
    ap.add_argument("--join", nargs="?", const="", metavar="ROOT",
                    help=t("cli.org_memory.join"))
    ap.add_argument("--decline", action="store_true", help=t("cli.org_memory.decline"))
    ap.add_argument("--status", action="store_true", help=t("cli.org_memory.status"))
    ap.add_argument("--init", metavar="ROOT", help=t("cli.org_memory.init"))
    ap.add_argument("--scan-tools", action="store_true",
                    help=t("cli.org_memory.scan_tools"))
    ap.add_argument("--project", metavar="ROOT", help=t("cli.org_memory.project"))
    ap.add_argument("--owner", metavar="USER", help=t("cli.org_memory.owner"))
    ap.add_argument("--dry-run", action="store_true", help=t("cli.org_memory.dry_run"))
    args = ap.parse_args()
    if args.join is not None:
        return cmd_join(args.join or None)
    if args.decline:
        return cmd_decline()
    if args.status:
        return cmd_status()
    if args.init:
        return cmd_init(args.init)
    if args.scan_tools:
        return cmd_scan_tools(args.project, args.owner, args.dry_run)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
