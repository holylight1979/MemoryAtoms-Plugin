"""atom_depends_infer：卡片正文點名的程式檔 → `- Depends:` 的 `path:` 條目（寫入漏斗自動掛、存量回填共用）。

做什麼：卡的知識段／行動段／Source 提到實際存在的檔，就把它當這張卡的依賴——程式改了，健檢
（tools/atom-health-check.py --deps-check）才會把卡標成可能過期。只讀檔案系統判存在，不寫任何東西。

抓法（三種寫法，都要檔真的存在才掛）：
  1. `hooks/`、`lib/`、`tools/`、`skills/` 開頭的相對路徑（前面可接 `~/.claude/`、`.claude/`）→ 程式碼根下存在 → `path:~/.claude/<rel>`
  2. 專案卡（卡在 `<專案>/.claude/memory/` 下）另抓一般相對路徑 → 專案根下存在 → `path:<相對專案根>`
  3. 裸檔名（`ups_inject.py`）→ 只在根層卡；整個程式碼根（版控追蹤檔）同名只有一個、且在那四個資料夾才掛，多命中不掛
  快照目錄（usage-watch 擴充的 vscode-data／browser-data／vscode-workspace）與 memory/episodic 自動生成卡一律不掛
寫法跟著讀 Depends 的程式走（lib/atom_spec.resolve_depends_path；健檢、過期判定、讀檔召回都用它）：
  `~/.claude/<四資料夾>/…` 安裝版解析到 ~/.claude（＝程式碼根），插件版改對插件程式碼根（資料根在別處也一樣）。
  本機解析不回程式碼根時（Claude Code 設定目錄另指、測試與乾淨環境的樹）程式檔一條都不掛：寫出去健檢會當缺檔。
卡跨機共享：一律寫 `~/.claude/<rel>` 或專案相對路徑，不寫本機絕對路徑。

公開函式：
  infer_depends(texts, atom_path, *, code_root=None) -> List[str]
  merge_depends(existing, inferred, atom_path) -> Optional[List[str]]
  with_auto_depends(depends, texts, atom_path, *, code_root=None) -> Optional[List[str]]
"""
from __future__ import annotations

import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from . import i18n
from .atom_spec import (
    DEPENDS_PATH_PREFIX, EPISODIC_DIR_NAME, depends_base_for_file, resolve_depends_path,
)
from .edition import CODE_ROOT
from .project_root import claude_data_dir
from .wg_platform import no_window_kwargs

# 和 lib/atom_spec._CODE_DEP_RE 同一組：插件版只把這四個資料夾的 `~/.claude/…` 對到程式碼根
CODE_DIRS = ("hooks", "lib", "tools", "skills")
_EXT = r"(?:py|js|md|json)"
# 1. 程式碼根四資料夾開頭；前一字元是 ASCII 字／點／連字號就不算（mylib/x.py 不是 lib/x.py）。前一字元是斜線另判（要接 .claude/）
_CODE_RE = re.compile(rf"(?<![A-Za-z0-9_\-.])((?:{'|'.join(CODE_DIRS)})/[\w\-/]+\.{_EXT})(?![A-Za-z0-9_])")
# 2. 專案相對路徑：只收 ASCII（\w 會把黏在前面的中文吃進路徑）
_REL_RE = re.compile(rf"(?<![A-Za-z0-9_\-./~:])((?:[A-Za-z0-9_\-.]+/)+[A-Za-z0-9_\-.]+\.{_EXT})(?![A-Za-z0-9_])")
# 3. 裸檔名：前後不接路徑字元
_BARE_RE = re.compile(rf"(?<![A-Za-z0-9_\-./])([A-Za-z0-9_\-]+\.{_EXT})(?![A-Za-z0-9_/])")
# 快照／暫存目錄：裡面的檔是別的程式的資料副本，不是卡要依賴的程式。
# vscode-data／browser-data／vscode-workspace 是 usage-watch 擴充放在資料根 tools/usage-snapshot/ 下的設定副本（安裝版就在程式碼根裡）
_SKIP_PARTS = {"__pycache__", "node_modules", ".trash", ".git", "vscode-data", "browser-data", "vscode-workspace"}
_HOME_FORM = "~/.claude/"   # 程式檔條目的寫法（解析規則見檔頭）


def _tracked_files(root: Path) -> Optional[List[str]]:
    """程式碼根的版控追蹤檔（相對、正斜線）；程式碼根本身不是 git repo、或 git 跑不動 → None。
    只認程式碼根自己的 .git：插件資料夾可能落在使用者別的 repo 底下，問 git 會拿到那個 repo 的（多半是空的）名冊。"""
    if not (root / ".git").exists():
        return None
    try:
        r = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, timeout=15,
                           **no_window_kwargs())
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return [f for f in r.stdout.decode("utf-8", "replace").split("\0") if f]


def _walk_files(root: Path) -> List[str]:
    """非 git 時的退路：根目錄的檔＋四資料夾與 workflow/（跳過快照與暫存目錄）。"""
    out: List[str] = []
    for p in root.iterdir() if root.is_dir() else []:
        if p.is_file():
            out.append(p.name)
    for d in CODE_DIRS + ("workflow",):
        base = root / d
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            rel = p.relative_to(root)
            if p.is_file() and not _SKIP_PARTS.intersection(rel.parts):
                out.append(rel.as_posix())
    return out


@lru_cache(maxsize=4)
def _bare_name_map(code_root: str) -> Dict[str, Tuple[str, ...]]:
    """檔名 → 程式碼根下所有同名檔（相對路徑）。名冊用版控追蹤檔（整個 repo，不只四資料夾：
    `settings.json` 有根目錄那份就不算唯一）；不是 git repo 才走目錄掃描。一個 process 掃一次。"""
    root = Path(code_root)
    files = _tracked_files(root)
    if files is None:
        files = _walk_files(root)
    out: Dict[str, List[str]] = {}
    for rel in files:
        parts = rel.split("/")
        if _SKIP_PARTS.intersection(parts):
            continue
        out.setdefault(parts[-1].lower(), []).append(rel)
    return {k: tuple(v) for k, v in out.items()}


def _same_dir(a: Optional[Path], b: Path) -> bool:
    try:
        return a is not None and a.resolve() == b.resolve()
    except OSError:
        return False


def _home_form_reaches(code_root: Path, base: Optional[Path]) -> bool:
    """`~/.claude/<四資料夾>/…` 在本機會被讀 Depends 的程式解析回 code_root 嗎（不會就不掛程式檔，見檔頭）。"""
    probe = resolve_depends_path(f"{_HOME_FORM}{CODE_DIRS[0]}", base=base)
    return _same_dir(probe.parent, code_root)


def _flatten(texts: Iterable[Optional[str]]) -> str:
    flat: List[str] = []
    for t in texts or []:
        if isinstance(t, (list, tuple)):
            flat.extend(str(x) for x in t if x)
        elif t:
            flat.append(str(t))
    return "\n".join(flat).replace("\\", "/")


def infer_depends(texts: Iterable[Optional[str]], atom_path: Path, *,
                  code_root: Optional[Path] = None) -> List[str]:
    """texts（知識行、行動行、Source…，可混 list）裡點名的存在檔 → `path:` 條目清單（照出現順序、去重）。
    atom_path 決定是根層卡還是專案卡（專案卡的相對路徑以專案根解析；檔不必已存在）。"""
    if EPISODIC_DIR_NAME in Path(atom_path).parts:
        return []   # memory/episodic 是程式自動生成的回合摘要，不是人寫的知識卡
    code_root = Path(code_root) if code_root else CODE_ROOT
    text = _flatten(texts)
    base = depends_base_for_file(Path(atom_path))
    project_root = None if (base is None or _same_dir(base, claude_data_dir()) or _same_dir(base, code_root)) else base
    code_ok = _home_form_reaches(code_root, base)

    out: List[str] = []

    def add(entry: str) -> None:
        if entry not in out:
            out.append(entry)

    for m in _CODE_RE.finditer(text):
        before = text[max(0, m.start() - len(".claude/")):m.start()].lower()
        explicit = before.endswith(".claude/")
        if before.endswith("/") and not explicit:
            continue   # 別的路徑裡的一段（src/lib/x.py），不是程式碼根
        rel = m.group(1)
        if _SKIP_PARTS.intersection(rel.split("/")):
            continue
        if project_root is not None and not explicit and (project_root / rel).is_file():
            add(f"{DEPENDS_PATH_PREFIX}{rel}")
        elif code_ok and (code_root / rel).is_file():
            add(f"{DEPENDS_PATH_PREFIX}{_HOME_FORM}{rel}")
    if project_root is not None:
        for m in _REL_RE.finditer(text):
            rel = m.group(1)
            while rel.startswith("./"):
                rel = rel[2:]
            if rel.startswith((".claude/memory/", "memory/")) or rel.startswith("../"):
                continue
            if (project_root / rel).is_file():
                add(f"{DEPENDS_PATH_PREFIX}{rel}")
    elif code_ok:
        names: Optional[Dict[str, Tuple[str, ...]]] = None   # 有裸檔名才建名冊（git 行程不白開）
        for m in _BARE_RE.finditer(text):
            if names is None:
                names = _bare_name_map(str(code_root))
            hits = names.get(m.group(1).lower(), ())
            if len(hits) == 1 and hits[0].split("/", 1)[0] in CODE_DIRS:
                add(f"{DEPENDS_PATH_PREFIX}{_HOME_FORM}{hits[0]}")
    return out


def _dep_key(entry: str, base: Optional[Path]) -> str:
    s = entry.strip()
    if not s.startswith(DEPENDS_PATH_PREFIX):
        return s
    try:
        return str(resolve_depends_path(s[len(DEPENDS_PATH_PREFIX):].strip(), base=base).resolve()).lower()
    except OSError:
        return s.lower()


def merge_depends(existing: Optional[List[str]], inferred: List[str], atom_path: Path) -> Optional[List[str]]:
    """既有 Depends 保留在前，推得的補在後；同一檔不同寫法（絕對／~／相對）算重複。
    沒推出新東西 → 原樣回 existing（None 仍是 None：呼叫端的三態不被改）。"""
    base = depends_base_for_file(Path(atom_path))
    merged = [e for e in (existing or []) if str(e).strip()]
    seen = {_dep_key(e, base) for e in merged}
    added = False
    for e in inferred:
        k = _dep_key(e, base)
        if k not in seen:
            seen.add(k)
            merged.append(e)
            added = True
    return merged if added else existing


def with_auto_depends(depends: Optional[List[str]], texts: Iterable[Optional[str]], atom_path: Path, *,
                      code_root: Optional[Path] = None) -> Optional[List[str]]:
    """寫入漏斗用：depends 合併正文推得的 path 條目。推斷出錯不擋寫入（回原值，stderr 告知）。"""
    try:
        return merge_depends(depends, infer_depends(texts, atom_path, code_root=code_root), atom_path)
    except Exception as e:   # 可觀測性鐵律：降級要出聲
        print(i18n.t("hook.atom_depends_infer.skipped", error=e), file=sys.stderr)
        return depends
