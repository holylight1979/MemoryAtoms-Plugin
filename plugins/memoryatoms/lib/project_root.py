"""project_root.py — 從 cwd 推導「記憶要歸哪個專案根」。

做什麼：沿祖先目錄讀 `.claude/project-tree.json` 宣告——根層列 `subs`、子層指 `root`——
決定 cwd 屬於哪個核心根層。沒有任何宣告時退回舊規則「最近四標記、最多 4 層」，行為不變。
怎麼跑：程式呼叫 resolve_project_root(cwd)；人用 `python tools/project-tree.py explain <cwd>`。

宣告檔欄位：root（相對本層、只准指祖先）、root_abs（本機絕對路徑，目錄存在時優先）、
subs（相對本層的路徑前綴，"*" 表全部）、standalone（true＝獨立、停止向上、不提問）。
路徑規則：沿字面路徑往上走，比對時才 resolve；回傳路徑保持字面形式。
排除：家目錄、資料根（claude_data_dir()，一般安裝就是 ~/.claude）本身、磁碟根永不當專案根，走到就停。
本模組只讀不寫；宣告檔的增刪改在 tools/project-tree.py。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def _edition_module():
    """同資料夾的 lib/edition.py：套件載入、頂層載入（lib 在 sys.path）都試過；本檔被按檔案路徑單獨載入
    （conftest.py、run_verify.py 的 _load_by_path）時 lib 不一定在 sys.path，就照本檔旁邊的檔案路徑載。"""
    try:
        from . import edition as module
        return module
    except ImportError:
        pass
    try:
        import edition as module
        return module
    except ImportError:
        import importlib.util
        import sys
        name = "_project_root_edition"
        if name not in sys.modules:
            spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("edition.py"))
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        return sys.modules[name]


def claude_data_dir() -> Path:
    """使用者資料根（memory、workflow、config、Logs、projects、atom 卡片）的唯一算法：
    環境變數 WG_CLAUDE_DIR → 插件 userConfig 的 data_dir（lib/edition.plugin_option：CLAUDE_PLUGIN_OPTION_DATA_DIR，
    插件版再看 settings.json 的 pluginConfigs）→ 插件版 ~/.memoryatoms、安裝版 ~/.claude。值一律去頭尾空白、展開 ~。
    hooks（wg_core.CLAUDE_DIR）與 tools 都呼叫這支；程式碼根另由各檔的 Path(__file__) 推。
    MIRROR: tools/workflow-guardian-mcp/lib/paths.js:CLAUDE_DIR。"""
    override = os.environ.get("WG_CLAUDE_DIR", "").strip()
    if override:
        return Path(os.path.expanduser(override))
    _edition = _edition_module()
    option = _edition.plugin_option("data_dir")
    if option:
        return Path(os.path.expanduser(option))
    return Path.home() / (".memoryatoms" if _edition.edition() == _edition.PLUGIN else ".claude")


def claude_config_dir() -> Path:
    """Claude Code 自己的設定目錄（settings.json、shell-snapshots、debug、plugins）：CLAUDE_CONFIG_DIR 有值就用，
    否則 Path.home()/".claude"。取值和 Claude Code 一樣（空字串當沒設、不去空白）；和資料根是兩件事，一般安裝才同一處。"""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def claude_global_config() -> Path:
    """Claude Code 的全域設定檔 .claude.json：CLAUDE_CONFIG_DIR 有值就在它底下，否則在家目錄本身（不是家目錄/.claude）。"""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home()) / ".claude.json"


# 向量服務埠沒設時由資料根路徑推：換資料根就換服務，兩套安裝不會串線。
# 範圍避開 3848／3849 與作業系統的臨時埠（Linux 32768 起、Windows 49152 起）。
VECTOR_PORT_BASE = 28000
VECTOR_PORT_SPAN = 1000


def _service_port_in(config: Optional[dict]) -> Optional[int]:
    """config 裡的 vector_search.service_port；沒寫或不是正整數回 None。"""
    section = (config or {}).get("vector_search")
    port = section.get("service_port") if isinstance(section, dict) else None
    return port if isinstance(port, int) and not isinstance(port, bool) and port > 0 else None


def _read_json_dict(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def vector_service_port(config: Optional[dict] = None) -> int:
    """向量服務埠的唯一算法。MIRROR: tools/workflow-guardian-mcp/lib/paths.js:vectorServicePort。
    config（整份 workflow/config.json 的內容）的 vector_search.service_port 有值就用；
    沒給 config 就讀資料根那份，config.local.json 同鍵蓋過；都沒寫 → 由資料根路徑推一個固定範圍內的穩定埠。"""
    root = claude_data_dir()
    if config is not None:
        port = _service_port_in(config)
    else:
        workflow = root / "workflow"
        port = _service_port_in(_read_json_dict(workflow / "config.local.json")) or             _service_port_in(_read_json_dict(workflow / "config.json"))
    if port:
        return port
    key = os.path.normpath(str(root)).replace("\\", "/").rstrip("/")
    if os.name == "nt":   # Windows 路徑不分大小寫：C:\X 與 c:\x 是同一個資料根
        key = key.lower()
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return VECTOR_PORT_BASE + int(digest[:8], 16) % VECTOR_PORT_SPAN


HOME = Path.home()
CLAUDE_DIR = claude_data_dir()
MEMORY_INDEX = "MEMORY.md"
DECL_NAME = "project-tree.json"
MAX_ROOT_HOPS = 3
LEGACY_MAX_LEVELS = 4
DECLARED_KINDS = ("self", "own-root", "ancestor-root", "ancestor-subs")


def _i18n():
    """語言表模組（用到才載：本模組是 i18n 自己的依賴，頂層匯入會成環）。"""
    try:
        from . import i18n
    except ImportError:  # 頂層模組載入（hooks／CLI 把 lib 加進 sys.path）
        import i18n
    return i18n


class _ClaimLabels:
    """claimed_by → 白話標籤（依目前語言取字）；用法同 dict 的 [] 與 get。"""
    _KEYS = ("self", "own-root", "ancestor-root", "ancestor-subs")

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        if key == "self":
            return _i18n().t("hook.project_root.claim_self")
        if key == "own-root":
            return _i18n().t("hook.project_root.claim_own_root")
        if key == "ancestor-root":
            return _i18n().t("hook.project_root.claim_ancestor_root")
        if key == "ancestor-subs":
            return _i18n().t("hook.project_root.claim_ancestor_subs")
        return default

    def __getitem__(self, key: str) -> str:
        if key not in self._KEYS:
            raise KeyError(key)
        return str(self.get(key))


CLAIM_LABEL = _ClaimLabels()


class _Record:
    """欄位與預設值照 _FIELDS 的輕量資料類（取代 dataclass：每個 hook 事件都 import 本模組，dataclasses 連帶 inspect 約 20 ms，
    HOOK_IMPORT_ON_USE）。只收關鍵字參數；list／dict 預設每次新建一份；== 比所有欄位。"""
    _FIELDS: Tuple[Tuple[str, object], ...] = ()

    def __init__(self, **kw: object) -> None:
        names = {n for n, _ in self._FIELDS}
        bad = set(kw) - names
        if bad:
            raise TypeError(_i18n().t("hook.project_root.record_no_field", cls=type(self).__name__, names=_i18n().t("hook.project_root.list_sep").join(sorted(bad))))
        for name, default in self._FIELDS:
            if name in kw:
                val = kw[name]
            elif default is _REQUIRED:
                raise TypeError(_i18n().t("hook.project_root.record_missing_field", cls=type(self).__name__, name=name))
            else:
                val = default() if callable(default) else default
            setattr(self, name, val)

    def __eq__(self, other: object) -> bool:
        return type(other) is type(self) and all(getattr(self, n) == getattr(other, n) for n, _ in self._FIELDS)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(" + ", ".join(f"{n}={getattr(self, n)!r}" for n, _ in self._FIELDS) + ")"


_REQUIRED = object()


class Declaration(_Record):
    _FIELDS = (("path", _REQUIRED), ("valid", _REQUIRED), ("root", None), ("root_abs", None), ("subs", list),
               ("standalone", False), ("error", ""), ("warnings", list), ("raw", dict))

    @property
    def declares_root(self) -> bool:
        return bool(self.root or self.root_abs)


class Resolution(_Record):
    # claimed_by：self | own-root | ancestor-root | ancestor-subs | nearest | none；via＝認領依據所在的層（印宣告行用）
    _FIELDS = (("path", _REQUIRED), ("claimed_by", _REQUIRED), ("nearest", _REQUIRED), ("via", None), ("candidates", list),
               ("warnings", list), ("infos", list), ("hops", list), ("standalone", False), ("fork_atoms", 0),
               ("fingerprint", ""), ("decl_files", list))

    @property
    def declared(self) -> bool:
        return self.claimed_by in DECLARED_KINDS


# ─── 路徑小工具 ───────────────────────────────────────────────────────────────


def _literal(p: str) -> Path:
    """字面絕對路徑（不解 junction、不展 8.3），只做 normpath。"""
    return Path(os.path.normpath(os.path.abspath(p)))


def _norm(p: Path) -> Path:
    try:
        return p.resolve()
    except OSError:
        return p


def _same(a: Path, b: Path) -> bool:
    return _norm(a) == _norm(b)


def _within(child: Path, parent: Path) -> bool:
    """child 等於 parent 或位於其下（resolve 後逐元件比對，Windows 不分大小寫）。"""
    c, p = _norm(child), _norm(parent)
    return c == p or p in c.parents


def _excluded(p: Path) -> bool:
    n = _norm(p)
    return n == _norm(HOME) or n == _norm(CLAUDE_DIR) or n.parent == n


def _has_memory(layer: Path) -> bool:
    return (layer / ".claude" / "memory" / MEMORY_INDEX).exists()


def declaration_path(layer: Path) -> Path:
    return layer / ".claude" / DECL_NAME


def _rel_ok(value: str) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    if os.path.isabs(value):
        return False
    return ".." not in Path(value).parts


def load_declaration(layer: Path) -> Optional[Declaration]:
    """讀本層宣告。無檔 → None；有檔但壞 → valid=False（呼叫端印 ⚠️、當無檔處理）。"""
    p = declaration_path(layer)
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        return Declaration(path=p, valid=False, error=_i18n().t("hook.project_root.decl_json_failed", error=e))
    if not isinstance(raw, dict):
        return Declaration(path=p, valid=False, error=_i18n().t("hook.project_root.decl_not_object"))
    d = Declaration(path=p, valid=True, raw=raw)
    for key in ("root", "root_abs"):
        v = raw.get(key)
        if v is None:
            continue
        if not isinstance(v, str) or not v.strip():
            return Declaration(path=p, valid=False, error=_i18n().t("hook.project_root.decl_field_not_str", field=key))
        setattr(d, key, v.strip())
    subs = raw.get("subs")
    if subs is not None:
        if not isinstance(subs, list):
            return Declaration(path=p, valid=False, error=_i18n().t("hook.project_root.decl_subs_not_list"))
        for s in subs:
            if s == "*":
                d.subs.append("*")
            elif _rel_ok(s):
                d.subs.append(s.strip())
            else:
                d.warnings.append(_i18n().t("hook.project_root.decl_sub_skipped", path=p, item=repr(s)))
    st = raw.get("standalone")
    if st is not None:
        if not isinstance(st, bool):
            return Declaration(path=p, valid=False, error=_i18n().t("hook.project_root.decl_standalone_bool"))
        d.standalone = st
    return d


def _subs_cover(layer: Path, decl: Declaration, cwd: Path) -> bool:
    if "*" in decl.subs:
        return True
    return any(_within(cwd, layer / s) for s in decl.subs if s != "*")


def _declared_target(layer: Path, decl: Declaration, out: Resolution) -> Optional[Path]:
    """本層 root/root_abs 指向的目錄；規則：root_abs 存在優先，root 只准指祖先。"""
    abs_target = None
    if decl.root_abs:
        cand = _literal(decl.root_abs)
        if cand.is_dir():
            abs_target = cand
    rel_target = None
    if decl.root:
        cand = Path(os.path.normpath(str(layer / decl.root)))
        if _within(layer, cand) and not _same(layer, cand):
            rel_target = cand
        else:
            out.warnings.append(_i18n().t("hook.project_root.root_not_ancestor", path=decl.path, root=repr(decl.root)))
    if abs_target and rel_target and not _same(abs_target, rel_target):
        out.warnings.append(_i18n().t("hook.project_root.root_abs_differs", path=decl.path, abs_target=abs_target, rel_target=rel_target))
    return abs_target or rel_target


def _follow_root_chain(layer: Path, decl: Declaration, out: Resolution) -> Optional[Path]:
    """跟著 root 鏈往上找終點根；失敗原因已寫進 out.warnings / out.infos。"""
    cur_layer, cur_decl = layer, decl
    seen = {_norm(layer)}
    for _ in range(MAX_ROOT_HOPS):
        target = _declared_target(cur_layer, cur_decl, out)
        if target is None:
            return None
        if _excluded(target):
            out.warnings.append(_i18n().t("hook.project_root.root_excluded", path=cur_decl.path, target=target))
            return None
        if _norm(target) in seen:
            out.warnings.append(_i18n().t("hook.project_root.root_cycle", path=cur_decl.path, target=target))
            return None
        seen.add(_norm(target))
        if not (target / ".claude").is_dir():
            out.infos.append(_i18n().t("hook.project_root.parent_not_checked_out", target=target))
            return None
        if _has_memory(target):
            return target
        tdecl = load_declaration(target)
        if tdecl is not None:
            out.decl_files.append(_decl_sig(tdecl))
        if tdecl is None or not tdecl.valid:
            why = tdecl.error if tdecl else _i18n().t("hook.project_root.no_memory_no_decl")
            out.warnings.append(_i18n().t("hook.project_root.root_target_bad", path=cur_decl.path, target=target, why=why))
            return None
        if tdecl.declares_root:
            cur_layer, cur_decl = target, tdecl
            continue
        out.infos.append(_i18n().t("hook.project_root.root_unsynced", root=target))
        return target
    out.warnings.append(_i18n().t("hook.project_root.root_too_many_hops", path=decl.path, hops=MAX_ROOT_HOPS))
    return None


def _cross_check_terminal(layer: Path, root: Path, out: Resolution) -> None:
    tdecl = load_declaration(root)
    if tdecl is not None:
        out.decl_files.append(_decl_sig(tdecl))   # 根層宣告改了也要讓指紋／快取失效
    if tdecl is None or not tdecl.valid or not tdecl.subs:
        return
    if not _subs_cover(root, tdecl, layer):
        rel = os.path.relpath(str(layer), str(root))
        try:
            from .wg_platform import paste_command
        except ImportError:  # 頂層模組載入（hooks 把 lib/ 放上 sys.path）
            from wg_platform import paste_command
        cmd = paste_command(Path(__file__).resolve().parents[1] / "tools" / "project-tree.py")
        out.warnings.append(_i18n().t("hook.project_root.sub_not_listed", root=root, rel=rel, cmd=cmd))


def _decl_sig(decl: Declaration) -> Tuple[str, float]:
    try:
        return (str(decl.path), decl.path.stat().st_mtime)
    except OSError:
        return (str(decl.path), 0.0)


def _fork_atom_count(layer: Path) -> int:
    idx = layer / ".claude" / "memory" / "_atom_index.json"
    if not idx.is_file():
        return 0
    try:
        data = json.loads(idx.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    atoms = data.get("atoms") if isinstance(data, dict) else None
    return len(atoms) if isinstance(atoms, (list, dict)) else 0


# ─── 舊規則（無宣告時的 fallback）─────────────────────────────────────────────


def has_strong_project_marker(root: Path) -> bool:
    """強標記：memory index / _AIDocs / .git / 有效宣告檔。**不含裸 .svn**。

    子目錄自己是一個 SVN 工作副本很常見（把 repo 的某一層單獨 checkout 出來用）。
    裸 .svn 與其他標記同級時，「最近標記」會停在那個子目錄，真正的專案根被蓋掉。
    """
    if (root / ".claude" / "memory" / MEMORY_INDEX).exists():
        return True
    if (root / "_AIDocs").is_dir():
        return True
    if (root / ".git").exists():
        return True
    d = load_declaration(root)
    return bool(d and d.valid)


def has_project_marker(root: Path) -> bool:
    """四標記 ∪ 有效宣告檔。取代 wg_core / atom_io 各自複製的判斷。"""
    return has_strong_project_marker(root) or (root / ".svn").exists()


def _legacy_nearest(start: Path) -> Optional[Path]:
    """無宣告認領時的專案根：最多 4 層，家目錄與磁碟根永不當專案根
    （~/.claude 本身仍可，核心層語意）。

    兩趟：強標記先掃滿整條鏈，掃不到才考慮裸 .svn。一趟掃描會讓巢狀的 SVN 工作副本
    蓋掉真正的專案根——專案記憶被切成第二棵殘缺的 .claude 樹，而且不會有任何錯誤訊號。
    """
    chain: List[Path] = []
    p = start
    for _ in range(LEGACY_MAX_LEVELS):
        if _norm(p) == _norm(HOME) or p.parent == p:
            break
        chain.append(p)
        p = p.parent
    for q in chain:
        if has_strong_project_marker(q):
            return q
    for q in chain:
        if (q / ".svn").exists():
            return q
    return None


# ─── 主流程 ───────────────────────────────────────────────────────────────────

_CACHE: Dict[str, Resolution] = {}


def clear_cache() -> None:
    _CACHE.clear()


def _cache_valid(res: Resolution) -> bool:
    for path, mtime in res.decl_files:
        try:
            if Path(path).stat().st_mtime != mtime:
                return False
        except OSError:
            if mtime != 0.0:
                return False
    return True


def resolve_project_root(cwd: Optional[str]) -> Resolution:
    if not cwd:
        return Resolution(path=None, claimed_by="none", nearest=None)
    start = _literal(cwd)
    hit = _CACHE.get(str(start))
    if hit is not None and _cache_valid(hit):
        return hit
    res = _resolve(start)
    _CACHE[str(start)] = res
    return res


def _resolve(start: Path) -> Resolution:
    out = Resolution(path=None, claimed_by="none", nearest=_legacy_nearest(start))
    layers: List[Path] = []
    p = start
    while True:
        if _excluded(p):
            break
        layers.append(p)
        if p.parent == p:
            break
        p = p.parent

    self_subs_only: Optional[Path] = None
    for idx, layer in enumerate(layers):
        is_self = idx == 0
        decl = load_declaration(layer)
        if decl is None:
            if not is_self and _has_memory(layer):
                out.candidates.append(layer)
            continue
        out.hops.append(layer)
        out.decl_files.append(_decl_sig(decl))
        if not decl.valid:
            out.warnings.append(_i18n().t("hook.project_root.decl_invalid_ignored", path=decl.path, error=decl.error))
            if not is_self and _has_memory(layer):
                out.candidates.append(layer)
            continue
        out.warnings.extend(decl.warnings)

        if decl.standalone:
            out.standalone = True
            break

        if self_subs_only is not None:
            # 本層已自立為根；只檢查外層是否也列了它（矛盾提示），不再認領。
            if decl.subs and _subs_cover(layer, decl, self_subs_only):
                out.warnings.append(_i18n().t("hook.project_root.standalone_conflict", layer_self=self_subs_only, outer=layer))
            continue

        if decl.declares_root:
            # 宣告 root 的層是子專案：它自己與底下全部都歸那個根
            target = _follow_root_chain(layer, decl, out)
            if target is None:
                if not is_self and _has_memory(layer):
                    out.candidates.append(layer)
                continue
            out.path = target
            out.claimed_by = "own-root" if is_self else "ancestor-root"
            out.via = layer
            _cross_check_terminal(layer, target, out)
            break

        if is_self:
            out.path = layer
            out.claimed_by = "self"
            out.via = layer
            if decl.subs:
                self_subs_only = layer
                continue
            break
        if decl.subs and _subs_cover(layer, decl, start):
            out.path = layer
            out.claimed_by = "ancestor-subs"
            out.via = layer
            break
        if _has_memory(layer):
            out.candidates.append(layer)

    if out.path is None:
        out.path = out.nearest
        out.claimed_by = "nearest" if out.nearest else "none"
    # 候選＝「有記憶層但沒宣告關係、且不是目前生效根」的上層；等於生效根就沒什麼可問
    out.candidates = [c for c in out.candidates if out.path is None or not _same(c, out.path)]

    if out.declared and out.path is not None:
        if not _has_memory(out.path) and not any(_i18n().t("hook.project_root.unsynced_marker") in i for i in out.infos):
            out.infos.append(_i18n().t("hook.project_root.root_unsynced", root=out.path))
        if out.nearest is not None and not _same(out.nearest, out.path):
            out.fork_atoms = _fork_atom_count(out.nearest)
            if out.fork_atoms:
                out.warnings.append(_i18n().t("hook.project_root.fork_atoms", nearest=out.nearest, count=out.fork_atoms, root=out.path))

    h = hashlib.sha1()
    # 指紋只看生效根、認領方式與宣告檔：cwd 原字串（大小寫、子目錄）變了不算換根，
    # 否則壓縮／resume 時 session 會誤判「專案根變更」重建 state、丟掉快照
    root_key = os.path.normcase(str(out.path)) if out.path is not None else ""   # Windows 不分大小寫
    h.update(f"{root_key}|{out.claimed_by}|".encode("utf-8", "replace"))
    for path, mtime in out.decl_files:
        h.update(f"{os.path.normcase(str(path))}:{mtime};".encode("utf-8", "replace"))
    out.fingerprint = h.hexdigest()[:16]
    return out


def describe(res: Resolution, cwd: str) -> List[str]:
    """人看的解析摘要（tools/project-tree.py explain / show 用）。"""
    lines = [f"cwd        : {cwd}",
             f"root       : {res.path}",
             f"claimed_by : {res.claimed_by}" + (f"（{CLAIM_LABEL[res.claimed_by]} @ {res.via}）" if res.via else ""),
             f"nearest    : {res.nearest}",
             f"standalone : {res.standalone}",
             f"fingerprint: {res.fingerprint}"]
    if res.hops:
        lines.append("hops       : " + " → ".join(str(h) for h in res.hops))
    if res.candidates:
        lines.append("candidates : " + ", ".join(str(c) for c in res.candidates))
    for i in res.infos:
        lines.append(f"info       : {i}")
    for w in res.warnings:
        lines.append(f"warning    : {w}")
    return lines
