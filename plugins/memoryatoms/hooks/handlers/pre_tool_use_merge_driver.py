"""
handlers/pre_tool_use_merge_driver.py — 索引三檔合併驅動閘（advisory-only，永不 deny）

做什麼：git／svn 指令前，索引三檔（MEMORY.md／_ATOM_INDEX.md／_atom_index.json）若仍 unmerged 就先跑 merge-atom-index.py --resolve；
合併類 git 指令前本機沒裝語意合併驅動就自動 --install。回提醒字串或 None。
誰叫它：handlers.pre_tool_use 的 handle_pre_tool_use（check_merge_driver）。
"""

import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional

from wg_core import find_svn_root, memory_dir_candidates
import i18n   # lib/：給人看的提示走語言表
from wg_platform import no_window_flags, paste_command
from atom_spec import ALL_INDEX_FILES  # noqa: E402  lib/（wg_core 已放進 sys.path）
from wg_lazy_re import lazy_func
# 指令拆段在 pre_tool_use_shell（守門家族，插件版不出廠）：名字留在模組上給測試換，呼叫時才載
_git_segments = lazy_func("handlers.pre_tool_use_shell", "_git_segments")
_resolve_run_cwd = lazy_func("handlers.pre_tool_use_shell", "_resolve_run_cwd")
_svn_segments = lazy_func("handlers.pre_tool_use_shell", "_svn_segments")
_tool_shell = lazy_func("handlers.pre_tool_use_shell", "_tool_shell")


# ─── 索引三檔合併驅動閘（advisory-only，永不 deny）─────────────────────────
# 多機共享記憶庫：兩機各加 atom 後 pull/rebase，索引三檔（MEMORY.md 計數表／_ATOM_INDEX.md／
# _atom_index.json）同區塊各加一列必衝突。兩層自動化（workflow/config.json merge_driver）：
#   (A) auto_install：合併類 git 指令（pull/merge/rebase/cherry-pick/stash pop|apply）前，
#       本機未裝語意合併驅動 → 自動 `merge-atom-index.py --install`（git 全域設定，各機一次）。
#   (B) auto_resolve：解衝突收尾指令（rebase/merge/cherry-pick --continue、commit、stash pop|apply）
#       前，索引三檔仍 unmerged → 先 `--resolve`（語意合併 stages 並 git add），再放行原指令。
#       不含 `git add`：使用者自己 add 索引檔即 git 已解除 stage，B 在此多餘。
# 唯一權威＝`git ls-files -u`（index-only、涵蓋 stash／worktree）。全程 fail-open、總時限 2.5s、
# Windows-safe（CREATE_NO_WINDOW；hook 跑在 pythonw 下，子行程直譯器改用同目錄 python.exe）。


_INDEX_FILE_NAMES = frozenset(ALL_INDEX_FILES)
# 用 hook 自己的檔案位置定位工具，不靠 HOME 推導（HOME 被覆寫的環境下 CLAUDE_DIR 會指錯地方）
_MERGE_TOOL = Path(__file__).resolve().parents[2] / "tools" / "merge-atom-index.py"
_MERGE_GATE_BUDGET_S = 2.5
_MERGE_RESOLVE_SUBS = {"rebase", "merge", "cherry-pick", "commit", "stash"}
_MERGE_INSTALL_SUBS = {"pull", "merge", "rebase", "cherry-pick", "stash"}
_MERGE_MANUAL_RESOLVE = i18n.t("hook.pre_tool_use_merge_driver.manual_resolve", cmd=paste_command(_MERGE_TOOL))
_MERGE_MANUAL_INSTALL = i18n.t("hook.pre_tool_use_merge_driver.manual_install", cmd=paste_command(_MERGE_TOOL))


def _hook_python_exe() -> str:
    """子行程直譯器（本體在 wg_core.hook_python_exe）：hook 在 pythonw.exe 下跑就改用同目錄 python.exe。"""
    from wg_core import hook_python_exe
    return hook_python_exe()


def _run_capture(args: List[str], cwd: str, timeout: float) -> subprocess.CompletedProcess:
    """A/B 共用的 Windows-safe 子行程：capture、UTF-8、errors=replace、timeout、不閃窗。
    不吞例外（TimeoutExpired 由 caller 轉成 ⚠ advisory）。"""
    return subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, cwd=cwd or None,
        creationflags=no_window_flags(),
    )


def _stash_pop_or_apply(tokens: List[str]) -> bool:
    return any(t.lower() in ("pop", "apply") for t in tokens[1:])


def _is_resolve_trigger(tokens: List[str]) -> bool:
    sub = tokens[0].lower()
    if sub == "commit":
        return True
    if sub in ("rebase", "merge", "cherry-pick"):
        return "--continue" in tokens
    if sub == "stash":
        return _stash_pop_or_apply(tokens)
    return False


def _is_install_trigger(tokens: List[str]) -> bool:
    sub = tokens[0].lower()
    if sub in ("pull", "merge", "rebase", "cherry-pick"):
        return True
    if sub == "stash":
        return _stash_pop_or_apply(tokens)
    return False


def _unmerged_index_files(run_cwd: str, timeout: float) -> Optional[List[str]]:
    """`git ls-files -u -z` 解出仍 unmerged 的索引三檔 repo 相對路徑。
    None＝查不到（非 repo／git 失敗）→ caller 當「無資訊」跳過；[]＝沒有。"""
    if not run_cwd or not os.path.isdir(run_cwd):
        return None
    r = _run_capture(["git", "ls-files", "-u", "-z"], run_cwd, timeout)
    if r.returncode != 0:
        return None
    found: List[str] = []
    for entry in (r.stdout or "").split("\0"):
        if "\t" not in entry:
            continue
        path = entry.split("\t", 1)[1]
        if path.rsplit("/", 1)[-1] in _INDEX_FILE_NAMES and path not in found:
            found.append(path)
    return found


_SVN_RESOLVE_SUBS = {"commit", "ci", "resolve", "resolved"}
_SVN_ACCEPT_PICKS = {"base", "mine-full", "theirs-full", "mine-conflict", "theirs-conflict", "mf", "tf", "mc", "tc"}


def _is_svn_resolve_trigger(tokens: List[str]) -> bool:
    """svn commit/ci 一律；svn resolve 只在沒明確選邊時（--accept working/postpone 或未給）——
    使用者已指定 mine-full/theirs-full 等就是他的決定，不搶先合併。svn update 不是觸發（無驅動可裝）。"""
    sub = tokens[0].lower()
    if sub in ("commit", "ci"):
        return True
    if sub not in ("resolve", "resolved"):
        return False
    for i, t in enumerate(tokens):
        if t.startswith("--accept="):
            val = t.split("=", 1)[1]
        elif t == "--accept" and i + 1 < len(tokens):
            val = tokens[i + 1]
        else:
            continue
        if val.lower() in _SVN_ACCEPT_PICKS:
            return False
    return True


def _svn_derived_exit_hint(run_cwd: str, rels: List[str]) -> str:
    """svn 索引自動合併只是暫時處置：_atom_index.json／_ATOM_INDEX.md 是衍生檔，正解是退出版控。
    把確切指令附在同一行（措辭與 lib/vcs_ignore 同源；SessionStart 的 [Guardian:VcsIgnore] 也會提）。
    rels＝相對 WC 根的路徑；沒有衍生檔或 lib 不在 → 空字串。"""
    try:
        from vcs_ignore import DERIVED_FILES, derived_exit_hint
    except ImportError:
        return ""
    root = find_svn_root(Path(run_cwd))
    if root is None:
        return ""
    paths = [root / rel for rel in rels if Path(rel).name in DERIVED_FILES]
    if not paths:
        return ""
    return i18n.t("hook.pre_tool_use_merge_driver.derived_temp", hint=derived_exit_hint("svn", paths))


def _svn_unmerged_index_files(run_cwd: str, timeout: float) -> Optional[List[str]]:
    """svn 工作副本裡 update 後仍衝突的索引三檔（相對 WC 根）。
    純檔案系統先找 .svn（不是 svn WC → None、零子行程），再只對 memory dir 候選跑 `svn status --xml`
    （整個 WC 的 status 要 3～6 秒，超出預算）。None＝查不到；[]＝沒有。"""
    if not run_cwd or not os.path.isdir(run_cwd):
        return None
    root = find_svn_root(Path(run_cwd))  # git 疊在 svn 上時 find_vcs_root 回 git，svn 側要另找
    if root is None:
        return None
    dirs = memory_dir_candidates(Path(run_cwd), root)
    if not dirs:
        return []
    r = _run_capture(["svn", "--non-interactive", "status", "--xml", "--", *map(str, dirs)], str(root), timeout)
    if r.returncode != 0 or not (r.stdout or "").strip():
        return None
    found: List[str] = []
    for ent in ET.fromstring(r.stdout).iter("entry"):
        ws = ent.find("wc-status")
        if ws is None or ws.get("item") != "conflicted":
            continue
        p = Path(ent.get("path", ""))
        if p.name not in _INDEX_FILE_NAMES:
            continue
        try:
            rel = (p if p.is_absolute() else root / p).resolve().relative_to(root.resolve()).as_posix()
        except (OSError, ValueError):
            continue
        if rel not in found:
            found.append(rel)
    return found


def _resolve_json(stdout: str) -> Optional[Dict[str, Any]]:
    """--resolve 的 stdout 契約是單行 JSON；保守取最後一個非空行解析。"""
    for ln in reversed((stdout or "").splitlines()):
        ln = ln.strip()
        if ln:
            try:
                d = json.loads(ln)
                return d if isinstance(d, dict) else None
            except ValueError:
                return None
    return None


def check_merge_driver(
    tool_name: str, tool_input: Dict[str, Any], cwd: str, config: Dict[str, Any]
) -> Optional[str]:
    """Bash/PowerShell git 指令前的索引三檔合併自動化。回 advisory 字串或 None；永不 deny。
    省錢階梯：工具名 → 字串含 git → 拆段命中 → 才動子行程；(B) 命中後不再跑 (A)。"""
    if tool_name not in ("Bash", "PowerShell"):
        return None
    mcfg = config.get("merge_driver") or {}
    auto_resolve = bool(mcfg.get("auto_resolve", True))
    auto_install = bool(mcfg.get("auto_install", True))
    if not (auto_resolve or auto_install):
        return None
    command = tool_input.get("command", "") or ""
    low = command.lower()
    if "git" not in low and "svn" not in low:
        return None
    try:
        git_segs = _git_segments(command, _MERGE_RESOLVE_SUBS | _MERGE_INSTALL_SUBS) if "git" in low else []
        svn_segs = _svn_segments(command, _SVN_RESOLVE_SUBS) if "svn" in low else []
        if not git_segs and not svn_segs:
            return None
        deadline = time.monotonic() + _MERGE_GATE_BUDGET_S

        def _left(cap: float) -> float:
            return max(0.05, min(cap, deadline - time.monotonic()))

        interp = _hook_python_exe()

        # (B) 解衝突收尾指令 → 索引三檔仍 unmerged（git stage／svn conflicted）就先 --resolve
        if auto_resolve:
            tagged = [("git", cd, tk) for cd, tk in git_segs] + [("svn", cd, tk) for cd, tk in svn_segs]
            for kind, repo_cd, tokens in tagged:
                is_git = kind == "git"
                if not (_is_resolve_trigger(tokens) if is_git else _is_svn_resolve_trigger(tokens)):
                    continue
                run_cwd = _resolve_run_cwd(repo_cd, cwd, _tool_shell(tool_name))
                finder = _unmerged_index_files if is_git else _svn_unmerged_index_files
                try:
                    unmerged = finder(run_cwd, _left(1.0))
                except subprocess.TimeoutExpired:
                    return i18n.t("hook.pre_tool_use_merge_driver.check_timeout", cmd='git ls-files' if is_git else 'svn status', manual=_MERGE_MANUAL_RESOLVE)
                if not unmerged:
                    continue
                try:
                    r = _run_capture(
                        [interp, str(_MERGE_TOOL), "--resolve", "--vcs", kind, "--cwd", run_cwd, "--quiet"],
                        run_cwd, _left(_MERGE_GATE_BUDGET_S),
                    )
                except subprocess.TimeoutExpired:
                    return i18n.t("hook.pre_tool_use_merge_driver.resolve_timeout", files=', '.join(unmerged), manual=_MERGE_MANUAL_RESOLVE)
                d = _resolve_json(r.stdout)
                if d is None:
                    tail = ((r.stderr or "").strip().splitlines() or [f"rc={r.returncode}"])[-1]
                    return i18n.t("hook.pre_tool_use_merge_driver.resolve_incomplete", detail=tail, manual=_MERGE_MANUAL_RESOLVE)
                remaining = [str(x) for x in (d.get("remaining") or [])]
                error = d.get("error")
                if remaining or error or r.returncode != 0:
                    why = error or ", ".join(remaining) or f"rc={r.returncode}"
                    return i18n.t("hook.pre_tool_use_merge_driver.resolve_incomplete", detail=why, manual=_MERGE_MANUAL_RESOLVE)
                resolved = [str(x) for x in (d.get("resolved") or [])]
                staged = [str(x) for x in (d.get("staged_user_version") or [])]
                parts = []
                if resolved:
                    parts.append(i18n.t("hook.pre_tool_use_merge_driver.merged_files", action='add' if is_git else i18n.t("hook.pre_tool_use_merge_driver.action_mark_resolved"), files=', '.join(resolved)))
                if staged:
                    parts.append(i18n.t("hook.pre_tool_use_merge_driver.staged_user_version", action='stage' if is_git else i18n.t("hook.pre_tool_use_merge_driver.action_mark_resolved"), files=', '.join(staged)))
                if not parts:
                    parts.append(i18n.t("hook.pre_tool_use_merge_driver.no_unmerged"))
                if not is_git:
                    hint = _svn_derived_exit_hint(run_cwd, resolved + staged)
                    if hint:
                        parts.append(hint)
                return "[Guardian:IndexConflict] " + i18n.t("hook.pre_tool_use_merge_driver.part_sep").join(parts)

        # (A) 合併類 git 指令 → 本機未裝驅動就自動 --install（只對第一個命中段做一次；svn 無驅動可裝）
        if auto_install:
            for repo_cd, tokens in git_segs:
                if not _is_install_trigger(tokens):
                    continue
                run_cwd = _resolve_run_cwd(repo_cd, cwd, _tool_shell(tool_name))
                try:
                    chk = _run_capture(
                        [interp, str(_MERGE_TOOL), "--is-installed", "--cwd", run_cwd],
                        cwd, _left(1.5),
                    )
                    if chk.returncode == 0:
                        return None
                    ins = _run_capture(
                        [interp, str(_MERGE_TOOL), "--install", "--quiet"], cwd, _left(1.5),
                    )
                except subprocess.TimeoutExpired:
                    return i18n.t("hook.pre_tool_use_merge_driver.install_check_timeout", manual=_MERGE_MANUAL_INSTALL)
                if ins.returncode == 0:
                    return i18n.t("hook.pre_tool_use_merge_driver.installed")
                tail = ((ins.stderr or ins.stdout or "").strip().splitlines() or [f"rc={ins.returncode}"])[-1]
                return i18n.t("hook.pre_tool_use_merge_driver.install_failed", tail=tail, manual=_MERGE_MANUAL_INSTALL)
    except Exception as e:
        try:
            sys.stderr.write(i18n.t("hook.pre_tool_use_merge_driver.check_error_failopen", err=e) + "\n")
        except OSError:
            pass
    return None
