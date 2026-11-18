"""
wg_roles.py — 身份、職能、裁決資格（多機共享、零宣告）

身份＝AD 帳號：get_current_user() 取 CLAUDE_USER → OS 登入帳號；取不到＝"unknown"
（哨兵值，wg_atoms.entry_visible 對它不開任何 personal，其他使用者的機器 getuser 失敗不得冒名）。

職能＝三層解析鏈，任一層失敗 fail-open 走下一層並 stderr 一行：
  1. role.md 人工覆寫：<proj>/.claude/memory/personal/<u>/role.md → ~/.claude/memory/personal/<u>/role.md，
     只看 `- Role: a, b` 一行（tools/init-roles.py --me 寫入；留空檔或刪檔即回到 AD）。
  2. AD 群組：whoami /groups（Windows 且有 USERDOMAIN 才跑，每個行程只 spawn 一次），群組名（<網域>\\ 後面那段）
     依對照表以子字串對映（先比長鍵）。對照表（load_group_map）：設定 roles.ad_group_map 有表就用（config.local.json
     蓋過 config.json）；空的再讀組織層 repo 的 config/roles.json（全組織共用，跟著組織層背景同步到每台）；都沒有＝這層
     不給職能（出廠就是空表）。組織層那份壞掉 → 開場紅字（lib/degraded）、這層當沒有。
     專案 MEMORY.md 有 `> Project-Code: PRJ001` 只取群組名以 `PRJ001_` 開頭的群組，沒宣告取聯集。
  3. 都沒有 → []：只看 shared／org／global，不預設 programmer（「不知道」不能被當成「是程式」擴大可見範圍）。

裁決資格＝config review.deciders（AD 帳號清單）；空＝人人可裁決。config 讀壞 → True 並 stderr。
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import wg_core
from wg_platform import no_window_kwargs, oem_codepage, IS_WINDOWS  # noqa: E402
import i18n  # lib/：給人看的提示走語言表

_DEFAULT_USER = "unknown"   # env 與 OS 都問不出身分時的中性值，不是任何人的帳號
_ROLE_LINE_RE = re.compile(r"^-\s*Role\s*:\s*(.*)$", re.IGNORECASE)
_PROJECT_CODE_RE = re.compile(r"^>\s*Project-Code:\s*(\S+)", re.MULTILINE)
# 組織層 repo 根底下的 AD 群組對照表，形狀同 config 的 roles 段（{"ad_group_map": {...}}）；
# 資料夾 config 列在 vcs_sync.org_extra_pathspecs，跟著組織層背景同步送到每台接上的機器
ORG_ROLES_FILE = Path("config") / "roles.json"

# whoami 群組原始清單，行程內只查一次（None＝還沒查；失敗也存 [] 不重複 spawn）
_ad_groups_cache: Optional[List[str]] = None


def _warn(msg: str) -> None:
    print(f"[wg_roles] {msg}", file=sys.stderr)


def get_current_user() -> str:
    """env CLAUDE_USER → OS 登入帳號（AD 帳號去網域）→ _DEFAULT_USER。

    personal/<user>/ 目錄名＝各機器 OS 帳號（bootstrap 慣例）。共用此核心的其他使用者的機器若拿到
    寫死的帳號，會把別人的 personal 當成自己的（可見性倒置、PersonalSync 指錯目錄），
    故此處不留任何人的帳號字面量；兩層都問不出身分才回中性的 _DEFAULT_USER。
    """
    u = os.environ.get("CLAUDE_USER")
    if u:
        return u
    try:
        import getpass
        u = getpass.getuser()
    except Exception:  # noqa: BLE001
        u = ""
    return u or _DEFAULT_USER


# ─── 裁決資格 ────────────────────────────────────────────────────────────────


def load_management_roster(cwd: str = "") -> List[str]:
    """config review.deciders；空＝人人可裁決。"""
    cfg = wg_core.load_config()
    if cfg.get("_config_parse_failed"):
        _warn(i18n.t("hook.wg_roles.workflow_config_json_failed"))
        return []
    return [str(u) for u in (cfg.get("review", {}) or {}).get("deciders") or []]


def is_management(cwd: str = "", user: str = "") -> bool:
    """deciders 空 → True；非空 → 使用者在名單內。config 讀失敗 → True（fail-open，已 stderr）。"""
    try:
        deciders = load_management_roster(cwd)
    except Exception as e:  # noqa: BLE001
        _warn(i18n.t("hook.wg_roles.failed_read_review_deciders", error=e))
        return True
    if not deciders:
        return True
    return (user or get_current_user()) in deciders


# ─── 職能：第 1 層 role.md ───────────────────────────────────────────────────


def role_md_candidates(cwd: str, user: str) -> List[Path]:
    """人工覆寫檔的查找順序：專案 → 全域。"""
    if not user or user == _DEFAULT_USER:
        return []
    out: List[Path] = []
    proj_mem = wg_core.get_project_memory_dir(cwd) if cwd else None
    if proj_mem:
        out.append(Path(proj_mem) / "personal" / user / "role.md")
    gl = wg_core.MEMORY_DIR / "personal" / user / "role.md"
    if gl not in out:
        out.append(gl)
    return out


def parse_role_md(path: Path) -> Optional[List[str]]:
    """只解析 `- Role:`；檔不存在或沒有非空 Role 行 → None（視為沒覆寫）。"""
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        m = _ROLE_LINE_RE.match(line.strip())
        if not m:
            continue
        roles = [r.strip() for r in m.group(1).split(",") if r.strip()]
        return roles or None
    return None


# ─── 職能：第 2 層 AD 群組 ───────────────────────────────────────────────────


def ad_available() -> bool:
    return IS_WINDOWS and bool(os.environ.get("USERDOMAIN"))


def _oem_encoding() -> str:
    """whoami 走主控台 OEM 碼頁（本機 cp950），不是 utf-8。"""
    return oem_codepage()


def _whoami_groups() -> List[str]:
    """跑 `whoami /groups /fo csv`，回每列第一欄（群組名）。失敗 raise 由呼叫端轉 stderr。"""
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "whoami.exe")
    kwargs: Dict[str, Any] = no_window_kwargs()
    r = subprocess.run([exe, "/groups", "/fo", "csv"], capture_output=True, timeout=3, **kwargs)
    if r.returncode != 0:
        raise RuntimeError(f"whoami exit {r.returncode}")
    text = r.stdout.decode(_oem_encoding(), errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    return [row[0] for row in rows[1:] if row]


def ad_groups() -> List[str]:
    """AD 群組名清單（行程內快取）；非 AD 機器或 whoami 失敗 → []（失敗 stderr 一次）。"""
    global _ad_groups_cache
    if _ad_groups_cache is not None:
        return _ad_groups_cache
    if not ad_available():
        _ad_groups_cache = []
        return _ad_groups_cache
    try:
        _ad_groups_cache = _whoami_groups()
    except Exception as e:  # noqa: BLE001
        _warn(i18n.t("hook.wg_roles.ad_group_lookup_whoami", error=e))
        _ad_groups_cache = []
    return _ad_groups_cache


def project_code(cwd: str) -> str:
    """專案 MEMORY.md 的 `> Project-Code: XXX`；沒有回空字串。"""
    proj_mem = wg_core.get_project_memory_dir(cwd) if cwd else None
    if not proj_mem:
        return ""
    md = Path(proj_mem) / wg_core.MEMORY_INDEX
    if not md.is_file():
        return ""
    m = _PROJECT_CODE_RE.search(md.read_text(encoding="utf-8-sig"))
    return m.group(1).strip() if m else ""


def _read_org_group_map(path: Path) -> Dict[str, str]:
    """組織層那份的 ad_group_map；讀不了、JSON 壞、ad_group_map 不是「字 → 字」的物件 → raise（呼叫端轉開場紅字）。"""
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    table = data.get("ad_group_map") if isinstance(data, dict) else None
    if not isinstance(table, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in table.items()):
        raise ValueError("ad_group_map must be an object of text -> text")
    return table


def load_group_map() -> Tuple[Dict[str, str], str, Optional[Path]]:
    """AD 群組對照表、出處（"config"／"org"／"none"）、組織層那份的位置（沒接組織層、或設定有表沒去讀時是 None）。

    先到先贏：設定 roles.ad_group_map 非空（config.local.json 蓋過 config.json；插件版含資料根 config.json）→ 整張用它；
    空的再讀組織層 repo 的 config/roles.json；都沒有 → 空表（這層不給職能）。
    沒接組織層、接了但沒放檔＝這個組織沒用這張表，跟出廠空表一樣不出聲；檔壞掉 → 開場紅字、當沒有。
    """
    table = (wg_core.load_config().get("roles", {}) or {}).get("ad_group_map") or {}
    if table:
        return table, "config", None
    root = wg_core.org_memory_root()
    if root is None:
        return {}, "none", None
    path = Path(root) / ORG_ROLES_FILE
    if not path.is_file():
        return {}, "none", path
    try:
        return _read_org_group_map(path), "org", path
    except (OSError, ValueError) as e:   # ValueError 含 JSON 壞與 UTF-8 解不開
        wg_core.degraded_notify("org_roles", reason=f"{type(e).__name__}: {e}", path=path)
        return {}, "none", path


def map_groups_to_roles(groups: List[str], group_map: Dict[str, str], code: str = "") -> List[str]:
    """群組名（去網域）依職能名子字串對映；code 非空只取 `<code>_` 開頭者。去重保序。"""
    keys = sorted((k for k in group_map if k), key=len, reverse=True)
    roles: List[str] = []
    for g in groups:
        if "\\" not in g:
            continue
        name = g.split("\\", 1)[1]
        if code and not name.startswith(code + "_"):
            continue
        hit = next((group_map[k] for k in keys if k in name), None)
        if hit and hit not in roles:
            roles.append(hit)
    return roles


# ─── 職能：解析鏈 ───────────────────────────────────────────────────────────


def resolve_roles_detail(cwd: str, user: str, full: bool = False) -> Dict[str, Any]:
    """三層各自看到什麼＋最終 roles／source。full=True（init-roles --status 對帳）即使 role.md 命中也照查 AD。"""
    detail: Dict[str, Any] = {
        "role_md": {"candidates": [], "path": None, "roles": None},
        "ad": {"available": ad_available(), "groups": [], "project_code": "", "roles": [],
               "map_source": "none", "map_path": None},
        "roles": [],
        "source": "none",
    }
    try:
        cands = role_md_candidates(cwd, user)
        detail["role_md"]["candidates"] = [str(p) for p in cands]
        for p in cands:
            roles = parse_role_md(p)
            if roles is None:
                continue
            detail["role_md"].update({"path": str(p), "roles": roles})
            detail.update({"roles": roles, "source": "role.md"})
            break
    except Exception as e:  # noqa: BLE001
        _warn(i18n.t("hook.wg_roles.role_md_failed_parse", error=e))
    if detail["source"] == "role.md" and not full:
        return detail

    try:
        groups = ad_groups()
        code = project_code(cwd)
        group_map, map_source, map_path = load_group_map()
        roles = map_groups_to_roles(groups, group_map, code)
        detail["ad"].update({"groups": groups, "project_code": code, "roles": roles,
                             "map_source": map_source, "map_path": str(map_path) if map_path else None})
        if roles and detail["source"] != "role.md":
            detail.update({"roles": roles, "source": "ad"})
    except Exception as e:  # noqa: BLE001
        _warn(i18n.t("hook.wg_roles.ad_role_mapping_failed", error=e))
    return detail


def load_user_role(cwd: str, user: str) -> Dict[str, Any]:
    """回 {"roles": [...], "source": "role.md"|"ad"|"none", "management": bool}。"""
    d = resolve_roles_detail(cwd, user)
    return {"roles": list(d["roles"]), "source": d["source"], "management": is_management(cwd, user)}


def bootstrap_personal_dir(cwd: str, user: str) -> Optional[Path]:
    """相容殼：SessionStart 仍呼叫。不再自動建目錄（personal/<u>/ 由第一次寫入或 --me 建立），回 None。"""
    return None
