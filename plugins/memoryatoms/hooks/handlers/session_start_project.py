"""
handlers/session_start_project.py — SessionStart 的專案根、組織層與開場指標行

做什麼：產生跟「這個專案／這台機器的記憶層」有關的開場行：專案根宣告與認領引導、
專案還沒有記憶層的初始化提示、尚未依 scope 整理的提示、組織層狀態與接入引導、_AIDocs 與本地範疇索引指標、
無部位表專案的 scaffold 提示。
誰叫它：handlers/session_start.py 的 handle_session_start（開場前段與新 session 分支）。
"""

import sys
from pathlib import Path
from typing import Any, Dict, List

from wg_core import (
    CLAUDE_DIR, CODE_DIR, MEMORY_DIR,
    _atom_debug_error,
    _is_under_claude_dir,
    find_vcs_root, is_transient_project_root,
    load_config, load_org_local,
)
from wg_platform import paste_command   # lib/：給人貼的指令（直譯器與腳本都是絕對路徑，不用 ~）
import i18n   # lib/：給人看的提示走語言表


def _scope_layout_advisory(project_mem_dir) -> list:
    """專案記憶尚未依 scope 分層整理 → 開場一行說明改動＋整理入口。

    記憶系統升級後（personal 只本人、專案規則進 shared 記提出者、他專案不注入），其他機器上
    的既有專案不會自己整理；「已整理」＝ _atom_index.json.layout 標記或 shared/_taxonomy.json
    （lib.atom_locations.scope_layout_classified）。純判定、fail-open。
    """
    try:
        if not project_mem_dir or not Path(project_mem_dir).is_dir():
            return []
        from atom_locations import scope_layout_classified
        if scope_layout_classified(Path(project_mem_dir)):
            return []
        return [i18n.t("hook.session_start_project.scope_layout")]
    except Exception as e:  # noqa: BLE001
        _atom_debug_error("session_start:scope_layout_advisory", e)
        return []


def _aidocs_opening_lines(aidocs_entries, project_root) -> List[str]:
    """開場 _AIDocs 一行：有索引就指 _INDEX.md（檔名清單不印，要用再 Read）；沒有就不印。
    _AIDocs 是選用的：要建，對 AI 說「讀懂這個專案」（/read-project 技能）。不每場提示建立——兩個版本都沒有建它的專用指令。"""
    if aidocs_entries:
        return [f"[AIDocs] {len(aidocs_entries)} docs → Read _AIDocs/_INDEX.md"]
    return []


def _local_catalog_lines(cwd: str) -> List[str]:
    """本地範疇 catalog 一行指標（只在核心環境 cwd∈~/.claude；整張表要用再 Read）。

    core catalog 走 CLAUDE.md @import memory/MEMORY.md；本地範疇明細在側檔 memory/_local_catalog.md。
    fail-safe：缺檔／非核心 → []（local atom 仍走 trigger 注入）。
    """
    try:
        if not _is_under_claude_dir(cwd):
            return []
        lc = MEMORY_DIR / "_local_catalog.md"
        if not lc.exists():
            return []
        return [i18n.t("hook.session_start_project.local_catalog", path=lc.as_posix())]
    except Exception as e:  # noqa: BLE001
        _atom_debug_error("realm:local_catalog_inject", e)
        return []


def _org_advisory(org_root, pool: Dict[str, Any]) -> List[str]:
    """組織層一行：`[Org] 組織層 N 顆（<root>）`。
    這台沒接上、使用者也還沒回答過（本機狀態檔沒有 enabled／declined）且共用 config 有 repo_url →
    要 AI 用 AskUserQuestion 問使用者放哪裡；**每個 session 都出，直到有答案**（接上或 --decline）。
    這幾行是給模型看的指示，不是給人看的通知：SessionStart 的輸出使用者看不到，只寫「對我說…」等於沒人會做。
    沒 repo_url 零 context。根未 checkout／索引缺 → 警告一行（fail-open 必浮訊號）。cwd 就是 org 根時池不另列 org 組，不報。"""
    if org_root is None:
        shared = load_config().get("org_memory")
        local = load_org_local()
        if not isinstance(shared, dict) or not shared.get("repo_url") or local.get("enabled") or local.get("declined"):
            return []
        cmd = paste_command(CODE_DIR / "tools" / "org-memory.py")
        default_root = str(shared.get("default_root") or "")
        if default_root.startswith("~"):
            default_root = str(Path(default_root).expanduser())  # config 寫家目錄相對路徑，問人時給展開後的真路徑
        opt_default = (i18n.t("hook.session_start_project.org_opt_default", root=default_root, cmd=cmd) if default_root else "")
        return [i18n.t("hook.session_start_project.org_ask", url=shared.get('repo_url'), opt_default=opt_default, cmd=cmd)]
    root = Path(org_root)
    if not (root / ".claude" / "memory").is_dir():
        return [i18n.t("hook.session_start_project.org_not_connected", root=root)]
    if not pool.get("org_base"):
        return []
    if not (Path(pool["org_base"]) / "memory" / "_atom_index.json").is_file():
        return [i18n.t("hook.session_start_project.org_index_missing", root=root)]
    return [i18n.t("hook.session_start_project.org_summary", n=len(pool.get('org') or []), root=root)]


def _project_memory_nudge(cwd: str, project_root, project_mem_dir) -> List[str]:
    """有版控的專案根、根下還沒有 .claude/memory → 一行提示怎麼一鍵初始化。其餘情況（沒版控、repo 子目錄、家目錄、暫存根）零輸出。"""
    if project_root is None:
        return []
    root = Path(project_root)
    if root in (CLAUDE_DIR, Path.home()) or is_transient_project_root(root):
        return []
    if project_mem_dir is not None and Path(project_mem_dir).is_dir():
        return []
    vcs = find_vcs_root(root)
    if vcs is None:
        # 插件版的新使用者常在還沒版控的資料夾試用：不提示的話 atom_write(scope=shared) 會直接失敗、卻沒人說怎麼辦。
        # 安裝版維持「沒版控零輸出」。只認 cwd 本身（不往上猜專案），免得在使用者的任意子資料夾亂冒提示。
        from edition import edition, PLUGIN   # lib/
        if edition() != PLUGIN or Path(cwd) != root:
            return []
    elif Path(vcs[1]) != root:
        return []
    command = paste_command(CODE_DIR / "tools" / "init-project-memory.py", "--cwd", f'"{root}"')
    return [i18n.t("hook.session_start_project.project_memory_nudge", root=root, command=command)]


def _project_root_notice(res, cwd: str) -> List[str]:
    """專案根宣告行（📍 認領／info、⚠️ 警告、❓ 引導）。沒宣告也沒候選 → 空清單，外部專案零 token。"""
    if res is None:
        return []
    lines: List[str] = []
    if res.declared and res.path is not None:
        from project_root import CLAIM_LABEL
        label = CLAIM_LABEL.get(res.claimed_by, res.claimed_by)
        if res.claimed_by != "self":
            lines.append(i18n.t("hook.session_start_project.subproject", cwd=cwd, path=res.path, label=label, via=res.via,
                                memory=res.path / '.claude' / 'memory'))
    for info in res.infos:
        lines.append(f"📍 [Guardian:ProjectRoot] {info}")
    for warn in res.warnings:
        lines.append(f"⚠️ [Guardian:ProjectRoot] {warn}")
    if res.claimed_by in ("nearest", "none") and res.candidates and not res.standalone:
        cands = i18n.t("hook.session_start_project.list_sep").join(str(c) for c in res.candidates)
        best = res.candidates[0]
        lines.append(i18n.t("hook.session_start_project.root_ask", cands=cands, cwd=cwd, best=best,
                            cmd=paste_command(CODE_DIR / "tools" / "project-tree.py")))
    return lines


def _first_session_hint_lines(root_res, cwd: str) -> list:
    """first_session_hint 掛 SessionStart：無部位表的專案給 scaffold 指令；表有「骨架」列就列每張卡的空段；
    表齊或不在任何專案 → 空。根層 cwd 以 ~/.claude 當根（它的表在 ~/.claude/.claude/overview-map.md）。fail-open。"""
    try:
        root = None
        if cwd:
            c = Path(cwd)
            if c == CLAUDE_DIR or CLAUDE_DIR in c.parents:
                root = CLAUDE_DIR
        if root is None and root_res is not None and getattr(root_res, "path", None):
            root = Path(root_res.path)
        if root is None:
            return []
        lib_dir = str(Path(__file__).resolve().parents[2] / "lib")   # 程式碼用本檔位置；CLAUDE_DIR 只拿來判根層與定位卡
        if lib_dir not in sys.path:
            sys.path.insert(0, lib_dir)
        from overview_scaffold import first_session_hint
        hint = first_session_hint(root, CLAUDE_DIR / "memory" if root == CLAUDE_DIR else None)
        return [hint] if hint else []
    except Exception as e:  # noqa: BLE001
        _atom_debug_error("session_start:first_session_hint", e)
        return []
