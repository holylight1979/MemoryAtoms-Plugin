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
    local_cards_recalled_at,
    find_vcs_root, is_transient_project_root,
    load_config, load_org_local,
)
from wg_platform import paste_command   # lib/：給人貼的指令（直譯器與腳本都是絕對路徑，不用 ~）
import i18n   # lib/：給人看的提示走語言表
from degraded import alert_line   # lib/：開場警告的單一出口（給 state 就記成結構化警告，不給照舊回字串）


def _scope_layout_advisory(project_mem_dir, state=None) -> list:
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
        return [ln for ln in [alert_line(state, "ScopeLayout", Path(project_mem_dir).as_posix(), "done", "project", i18n.t("hook.session_start_project.scope_layout"), i18n.t("hook.phase_alerts.fix.p01"))] if ln]
    except Exception as e:  # noqa: BLE001
        _atom_debug_error("session_start:scope_layout_advisory", e)
        return []


def _aidocs_opening_lines(aidocs_entries, project_root) -> List[str]:
    """開場 _AIDocs 一行：有索引就指 _INDEX.md（檔名清單不印，要用再 Read）；沒有就不印。
    _AIDocs 是選用的：要建，對 AI 說「讀懂這個專案」（/read-project 技能）。不每場提示建立——兩個版本都沒有建它的專用指令。"""
    if aidocs_entries:
        return [i18n.t("hook.session_start_project.aidocs_index", n=len(aidocs_entries))]
    return []


def _local_catalog_lines(cwd: str) -> List[str]:
    """本地範疇 catalog 一行指標（只在本地卡想得起來的地方：資料根底下或記憶系統原始碼樹裡；整張表要用再 Read）。

    core catalog 走 CLAUDE.md @import memory/MEMORY.md；本地範疇明細在側檔 memory/_local_catalog.md（資料根那份）。
    fail-safe：缺檔／想不起本地卡的地方 → []（local atom 仍走 trigger 注入）。
    """
    try:
        if not local_cards_recalled_at(cwd):
            return []
        lc = MEMORY_DIR / "_local_catalog.md"
        if not lc.exists():
            return []
        return [i18n.t("hook.session_start_project.local_catalog", path=lc.as_posix())]
    except Exception as e:  # noqa: BLE001
        _atom_debug_error("realm:local_catalog_inject", e)
        return []


def _org_advisory(org_root, pool: Dict[str, Any], state=None) -> List[str]:
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
        # 指令型：叫模型第一則回覆前問使用者，不是警告；一律開場，沒有解法句
        return [alert_line(state, "Org", "org_connect", "open", "root",
                           i18n.t("hook.session_start_project.org_ask", url=shared.get('repo_url'), opt_default=opt_default, cmd=cmd))]
    root = Path(org_root)
    if not (root / ".claude" / "memory").is_dir():
        return [ln for ln in [alert_line(state, "Org", "org_connect", "done", "root", i18n.t("hook.session_start_project.org_not_connected", root=root), i18n.t("hook.phase_alerts.fix.p05"))] if ln]
    if not pool.get("org_base"):
        return []
    if not (Path(pool["org_base"]) / "memory" / "_atom_index.json").is_file():
        return [ln for ln in [alert_line(state, "Org", "org_index", "done", "root", i18n.t("hook.session_start_project.org_index_missing", root=root), i18n.t("hook.phase_alerts.fix.p06", root=root))] if ln]
    return [i18n.t("hook.session_start_project.org_summary", n=len(pool.get('org') or []), root=root)]


def _project_memory_nudge(cwd: str, project_root, project_mem_dir, state=None) -> List[str]:
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
    return [ln for ln in [alert_line(state, "ProjectMemory", root.as_posix(), "open", "project", i18n.t("hook.session_start_project.project_memory_nudge", root=root, command=command), i18n.t("hook.phase_alerts.fix.p02"))] if ln]


def _has_paths_header(text: str) -> bool:
    """rules file with a `paths:` key in its leading front matter = conditionally loaded."""
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    for ln in lines[1:]:
        if ln.strip() == "---":
            return False
        if ln.lstrip().startswith("paths:"):
            return True
    return False


def _project_rules_chars(project_root) -> int:
    """Characters of the project's always-loaded rule files: CLAUDE.md, CLAUDE.local.md, .claude/CLAUDE.md,
    plus .claude/rules/*.md without a `paths:` header. Counts only; never judges content."""
    root = Path(project_root)
    files = [root / "CLAUDE.md", root / "CLAUDE.local.md", root / ".claude" / "CLAUDE.md"]
    rules = root / ".claude" / "rules"
    if rules.is_dir():
        files.extend(sorted(rules.glob("*.md")))
    total = 0
    for f in files:
        try:
            if not f.is_file():
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if f.parent == rules and _has_paths_header(text):
            continue
        total += len(text)
    return total


def _project_rules_advisory(project_root, state: Dict[str, Any], config: Dict[str, Any], record: bool = False) -> List[str]:
    """One line when the project's always-loaded rules exceed project_rules.warn_chars (0 = off); once per session.
    Skipped for the memory system's own tree. Size only, no content judgement, no file changes. Fail-open.
    record=True: log it as a phase-end alert on state (raised once when the phase wraps up, with a fix); default False keeps the old opening line."""
    try:
        if not project_root or state.get("project_rules_warned"):
            return []
        cfg = config.get("project_rules") if isinstance(config.get("project_rules"), dict) else {}
        limit = int(cfg.get("warn_chars", 8000))
        root = Path(project_root)
        if limit <= 0 or (root / "hooks" / "workflow-guardian.py").is_file():
            return []
        chars = _project_rules_chars(root)
        if chars <= limit:
            return []
        state["project_rules_warned"] = True
        text = i18n.t("hook.session_start_project.project_rules_bloat", chars=chars, tok=chars // 2)
        return [ln for ln in [alert_line(state if record else None, "ProjectRules", root.as_posix(), "done", "project", text, i18n.t("hook.phase_alerts.fix.p03"))] if ln]
    except Exception as e:  # noqa: BLE001
        _atom_debug_error("session_start:project_rules_advisory", e)
        return []


def _project_root_notice(res, cwd: str, state=None) -> List[str]:
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
    for i, info in enumerate(res.infos):
        lines.append(alert_line(state, "ProjectRoot", f"info{i}", "report", "project", f"📍 [Guardian:ProjectRoot] {info}"))
    for i, warn in enumerate(res.warnings):   # 宣告不成立＝記憶歸屬錯，維持開場；對象用序號（警告種類很多，文字裡有路徑）
        lines.append(alert_line(state, "ProjectRoot", f"declaration{i}", "open", "project", f"⚠️ [Guardian:ProjectRoot] {warn}", i18n.t("hook.phase_alerts.fix.p08")))
    if res.claimed_by in ("nearest", "none") and res.candidates and not res.standalone:
        cands = i18n.t("hook.session_start_project.list_sep").join(str(c) for c in res.candidates)
        best = res.candidates[0]
        # 指令型：叫模型問使用者四選一，不是警告；「這次先不決定」下次會再問
        lines.append(alert_line(state, "ProjectRoot", "root_ask", "open", "project",
                                i18n.t("hook.session_start_project.root_ask", cands=cands, cwd=cwd, best=best,
                                       cmd=paste_command(CODE_DIR / "tools" / "project-tree.py"))))
    return [ln for ln in lines if ln]


def _first_session_hint_lines(root_res, cwd: str, state=None) -> list:
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
        from overview_scaffold import first_session_hint, MAP_REL
        hint = first_session_hint(root, CLAUDE_DIR / "memory" if root == CLAUDE_DIR else None)
        if not hint:
            return []
        scope = "root" if root == CLAUDE_DIR else "project"
        if not (root / MAP_REL).is_file():   # 還沒有部位表：引導新專案，完工時提一次
            line = alert_line(state, "OverviewHub", "overview_map", "done", scope, hint, i18n.t("hook.phase_alerts.fix.p10"))
        else:   # 部位表有骨架列：改那個部位前就要先補卡，維持開場
            line = alert_line(state, "OverviewHub", "overview_skeleton", "open", scope, hint, i18n.t("hook.phase_alerts.fix.p11"))
        return [line] if line else []
    except Exception as e:  # noqa: BLE001
        _atom_debug_error("session_start:first_session_hint", e)
        return []
