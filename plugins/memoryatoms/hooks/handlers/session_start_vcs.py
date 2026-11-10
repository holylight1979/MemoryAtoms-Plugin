"""
handlers/session_start_vcs.py — SessionStart 的記憶庫版控（同步、衝突、退出版控）

做什麼：開場時背景起拉取同步與來源回填；回報未 push／拉入／索引衝突（git 與 SVN）／衍生檔退出版控／
本人 personal 卡的版控缺口。這些都是唯讀或 fail-open 的提示，異常才出行。
誰叫它：handlers/session_start.py 的 handle_session_start；session_start_index.py 取用衍生檔名單。
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

from wg_core import (
    CLAUDE_DIR, CODE_DIR, WORKFLOW_DIR, MEMORY_DIR,
    _atom_debug_error,
    find_vcs_root, find_svn_root, memory_dir_candidates,
)
from wg_platform import no_window_flags, background_spawn_kwargs, paste_command  # noqa: E402
_MERGE_CMD = paste_command(CODE_DIR / "tools" / "merge-atom-index.py")   # 提示裡給人貼的合併指令（實際直譯器＋腳本絕對路徑）
from atom_spec import ALL_INDEX_FILES, INDEX_FILES   # lib/：共用名字
import i18n   # lib/：給人看的提示走語言表

try:
    from vcs_ignore import (  # lib/：衍生檔退出版控（wg_core 已把 CODE_DIR/lib 放上 sys.path）
        DERIVED_DIRS as _DERIVED_DIR_NAMES,
        DERIVED_FILES as _DERIVED_INDEX_NAMES,
        rebuild_hint as _rebuild_manual,
        advisory_lines as _vcs_ignore_lines,
        derived_exit_hint as _derived_exit_hint,
        under_claude_dir as _under_claude_dir,
    )
except ImportError as _e:  # lib 不在（hooks 升了、lib 沒升）：缺檔重建與衝突提示照舊，只少「退出版控」那段；留痕不無聲
    _atom_debug_error("session_start:vcs_ignore_import", _e)
    _DERIVED_INDEX_NAMES = ("_atom_index.json", "_ATOM_INDEX.md")
    _DERIVED_DIR_NAMES = ("_staging", "episodic", "vector-db")
    def _rebuild_manual(mem) -> str:   # 同 vcs_ignore.rebuild_hint
        return paste_command(CODE_DIR / "tools" / "sync-atom-index.py", "--rebuild", "--memory-dir", f'"{mem}"')
    _vcs_ignore_lines = None
    _derived_exit_hint = None
    _under_claude_dir = lambda _p: True  # noqa: E731  分不出是不是根層記憶 → 不做過期檢查，只保留缺檔重建


def _spawn_pull_sync(session_id: str, cwd: str, config: Dict[str, Any]) -> int:
    """SessionStart 的拉取觸發：`vcs_sync.enabled` 且 `vcs_sync.pull.enabled` 才 spawn（reason="pull"）。
    fail-open：任何失敗只進 atom-debug log（spawn 自己會留 `.unpushed`／last_error 給 advisory）。回 pid（0＝未起）。"""
    try:
        import wg_vcs_sync as _vs
        vs = _vs.vcs_sync_config(config or {})
        if not vs.get("enabled", True) or not (vs.get("pull") or {}).get("enabled", True):
            return 0
        return _vs.spawn_vcs_sync(session_id, cwd, reason="pull", config=config)
    except Exception as e:
        _atom_debug_error("session_start:spawn_pull_sync", e)
        return 0


def _maybe_spawn_provenance_backfill(config: Dict[str, Any]) -> int:
    """舊 atom 來源回填：有比上次更新的 transcript 才 detached 跑 `tools/atom-provenance-backfill.py --apply --quiet`。
    判定只比 mtime（<50ms）；config `provenance.backfill.enabled=false` 關。fail-open：失敗只進 atom-debug log。回 pid（0＝未起）。"""
    try:
        prov = (config or {}).get("provenance") or {}
        if not prov.get("enabled", True) or not (prov.get("backfill") or {}).get("enabled", True):
            return 0
        import importlib.util
        tool = CODE_DIR / "tools" / "atom-provenance-backfill.py"
        if not tool.exists():
            return 0
        spec = importlib.util.spec_from_file_location("atom_provenance_backfill", tool)
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        sys.modules["atom_provenance_backfill"] = mod
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        if not mod.needs_run():
            return 0
        import subprocess as _sp
        import wg_vcs_sync as _vs
        kwargs: Dict[str, Any] = background_spawn_kwargs(detached=True)
        mod.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(mod.LOG_PATH, "a", encoding="utf-8", newline="\n") as log_fh:
            proc = _sp.Popen([_vs._gui_python(), str(tool), "--apply", "--quiet"],
                             stdin=_sp.DEVNULL, stdout=_sp.DEVNULL, stderr=log_fh,
                             env=_vs.worker_env(), **kwargs)
        return int(proc.pid or 0)
    except Exception as e:
        _atom_debug_error("session_start:provenance_backfill", e)
        return 0


def _unpushed_advisory() -> list:
    """本地有已 commit 未 push／worker 留下 `.unpushed` 標記的 root → advisory 行（無則回 []）。
    拉側（roots.json 的 last_pull／pulled_commits／pull_error 與 `.behind` 標記）也在這裡報：拉入 N 筆 → 一行提醒
    候選池以本次載入快照為準；`.behind` → 一行理由。pull 欄位與 push 的 last_error 分欄，解除 `.unpushed` 的邏輯不碰它們。

    存在理由：vcs-sync worker 在背景 commit+push 記憶庫，push 守門擋下（本地有未發布程式碼
    commit）或 push／svn commit 失敗時只留標記與 log、當下沒人看得到。這裡在下個 session 開頭補上
    可見性，讓「背景 fail-open」不變成「永遠沒人發現」（可觀測性鐵律）。

    範圍：workflow/vcs-sync/roots.json 列出的每個 root（根層 + 專案）；roots.json 尚無根層時退回
    只查 ~/.claude。每 root 依序看：git `rev-list --count @{u}..HEAD`、`.unpushed` 標記（查詢成功且
    ahead=0 → 已沒有東西待推，不論使用者是補推原 HEAD 還是另開 commit，都算已解決：刪標記並清 roots.json
    的 last_error；查詢失敗 rc≠0 不得當 ahead=0——那是「不知道」，標記照報、不刪）、roots.json 的
    last_error（skip／索引失敗／spawn 失敗）、`.req/` 內無人消費的請求（含 inflight 殘留：worker 沒起或中途死）。
    除了清已解決的標記／last_error 外只讀不寫；單一 root 失敗不影響其他 root——沒有 upstream / 不是 repo /
    git 不在都算正常（查不到 ahead 就不報 ahead）。
    """
    try:
        import subprocess
        from pathlib import Path as _P
        try:
            import wg_vcs_sync as _vs
            roots = _vs.load_roots()
        except Exception as e:
            _atom_debug_error("session_start:unpushed_advisory:roots", e)
            _vs, roots = None, {}
        entries = [(_P(k), v or {}) for k, v in roots.items()]
        if not any(r.resolve() == CLAUDE_DIR.resolve() for r, _ in entries) and (CLAUDE_DIR / ".git").exists():
            entries.insert(0, (CLAUDE_DIR, {}))

        def _git(root: _P, *args: str):
            """回 (成功?, stdout)。失敗（無 upstream／不是 repo／git 不在）與「0」必須分得開。"""
            r = subprocess.run(
                ["git", "-C", str(root), *args],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=5, creationflags=no_window_flags())
            return r.returncode == 0, (r.stdout or "").strip()

        lines = []
        for root, info in entries:
            vcs = info.get("vcs", "git")
            last_error = info.get("last_error")
            label = "~/.claude" if root.resolve() == CLAUDE_DIR.resolve() else root.as_posix()
            try:
                rec = _vs.read_unpushed_record(root) if _vs else None
                is_git = vcs == "git" and (root / ".git").exists()
                ahead, ahead_known = 0, False
                if is_git:
                    ok, out = _git(root, "rev-list", "--count", "@{u}..HEAD")
                    if ok and out.isdigit():
                        ahead, ahead_known = int(out), True
                if rec and ahead_known and ahead == 0:
                    # 查詢成功且沒有東西待推 → 已解決（使用者補推原 HEAD 也算）。先清 roots.json 的
                    # last_error（可能因 roots.lock 逾時失敗），成功才刪標記；失敗就留著下次再清，
                    # 否則標記沒了、last_error 卻永遠清不掉。
                    cleared = True
                    if last_error:
                        cleared = bool(_vs.update_root_record(
                            _vs.SyncTarget(vcs, root, list(info.get("pathspecs") or [])), last_error=None))
                        if cleared:
                            last_error = None
                    if cleared:
                        _vs.clear_unpushed(root)
                        rec = None
                pending = _vs.pending_requests(root, include_inflight=True) if _vs else 0
                orphan = pending > 0 and _vs is not None and not _vs.lock_is_live(root)
            except Exception as e:
                _atom_debug_error("session_start:unpushed_advisory:root", e)
                lines.append(i18n.t("hook.session_start_vcs.unpushed_check_failed_label", label=label, etype=type(e).__name__))
                continue
            reason = (rec or {}).get("reason")
            if ahead > 0:
                lines.append(i18n.t("hook.session_start.unpushed_commits", label=label, ahead=ahead))
            elif reason:
                lines.append(i18n.t("hook.session_start.memory_not_versioned", label=label, reason=reason[:80]))
            elif last_error:
                lines.append(i18n.t("hook.session_start_vcs.last_sync_incomplete", label=label, err=str(last_error)[:80]))
            if orphan:
                lines.append(i18n.t("hook.session_start_vcs.sync_orphan", label=label, pending=pending))
            lines.extend(_pull_advisory_lines(_vs, root, label, info))
        return lines
    except Exception as e:
        _atom_debug_error("session_start:unpushed_advisory", e)
        # fail-open 但要告知：這個檢查曾靜默 crash 三週沒人知道
        return [i18n.t("hook.session_start_vcs.unpushed_check_failed", etype=type(e).__name__)]


def _pull_advisory_lines(_vs, root, label: str, info: Dict[str, Any]) -> list:
    """拉側兩行（各自可缺）：上次拉入 N>0 筆 → 提醒候選池是本次載入快照；`.behind` 標記（或只剩 pull_error）→ 理由。
    「拉入 N 筆」一次性：報過就把 last_pull 記進 roots.json pull_reported_at，同一次 last_pull 不再報（cooldown 內
    的 reason=pull 請求不會覆寫 last_pull，沒有這個欄位會每個 session 重複報）。"""
    try:
        lines = []
        pulled = info.get("pulled_commits") or 0
        last_pull = info.get("last_pull")
        if last_pull and pulled > 0 and info.get("pull_reported_at") != last_pull:
            lines.append(i18n.t("hook.session_start_vcs.pulled", label=label, pulled=pulled, when=str(last_pull)[:19]))
            # 「只報一次」盡力而為：寫 pull_reported_at 失敗（roots.lock 逾時，回 False、已進 atom-debug log）就不算
            # 已報，下個 session 會再報同一次拉入；兩個 session 同時讀到同一份快照也會各報一次。重報一行提醒無害，
            # 不為此在讀→判→寫之間加鎖。
            if _vs:
                _vs.update_root_record(_vs.SyncTarget(info.get("vcs", "git"), root, list(info.get("pathspecs") or [])),
                                       pull_reported_at=last_pull)
        rec = _vs.read_behind_record(root) if _vs else None
        reason = (rec or {}).get("reason") or info.get("pull_error")
        if reason:
            lines.append(i18n.t("hook.session_start.memory_behind", label=label, reason=str(reason)[:100]))
        return lines
    except Exception as e:
        _atom_debug_error("session_start:pull_advisory", e)
        return [i18n.t("hook.session_start_vcs.pull_check_failed", label=label, etype=type(e).__name__)]


def _index_conflict_advisory(cwd: str) -> list:
    """開場 advisory：上個 session 的 pull/rebase 卡在索引三檔衝突、還沒解就關掉 → 這裡浮出一行。

    exists()-first 省錢：先一次 `git rev-parse --git-dir`（worktree 相容），只有 MERGE_HEAD／
    CHERRY_PICK_HEAD／rebase-merge／rebase-apply 任一存在（真的卡在合併中）才跑 `git ls-files -u`。
    唯讀 git；非 repo／git 不在／任何失敗 → []。PreToolUse 的 check_merge_driver 會在下一個
    `rebase --continue`／`commit` 前自動 --resolve，這行只是讓人先知道現況。
    """
    lines: list = []
    try:
        if not cwd or not Path(cwd).is_dir():
            return []
        # svn 側獨立找：git 疊在 svn 上（同層兩個都有）時 find_vcs_root 回 git，svn 衝突會被略過
        svn_root = find_svn_root(Path(cwd))
        if svn_root is not None:
            lines += _svn_index_conflict_advisory(cwd, svn_root)
        vcs = find_vcs_root(Path(cwd))  # 零子行程：非工作區直接零行
        if vcs is None or vcs[0] != "git":
            return lines
        r = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=2, cwd=cwd, creationflags=no_window_flags())
        if r.returncode != 0:
            return lines
        gitdir = Path((r.stdout or "").strip())
        if not gitdir.is_absolute():
            gitdir = Path(cwd) / gitdir
        if not any((gitdir / n).exists()
                   for n in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "rebase-merge", "rebase-apply")):
            return lines
        r = subprocess.run(
            ["git", "ls-files", "-u", "-z"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=2, cwd=cwd, creationflags=no_window_flags())
        if r.returncode != 0:
            return lines
        index_names = set(ALL_INDEX_FILES)
        names = sorted({
            entry.split("\t", 1)[1].rsplit("/", 1)[-1]
            for entry in (r.stdout or "").split("\0") if "\t" in entry
        } & index_names)
        if names:
            lines.append(i18n.t("hook.session_start_vcs.index_unmerged", names=', '.join(names), cmd=_MERGE_CMD))
        return lines
    except Exception as e:
        _atom_debug_error("session_start:index_conflict_advisory", e)
        return lines


_SVN_INDEX_NAMES = INDEX_FILES


def _svn_index_conflict_advisory(cwd: str, root: Path) -> list:
    """SVN 工作副本：update 衝突會在原檔旁留下 <檔>.mine。memory dir 候選裡有就提示（零子行程）。

    索引三檔與 atom 分開講。索引裡的 _atom_index.json／_ATOM_INDEX.md 是衍生檔，衝突的正解是退出版控
    （指令與 [Guardian:VcsIgnore] 同源），hook 的自動合併只是暫時處置；atom 衝突要人判斷，
    而且在解完之前 SessionEnd 的反向連結修補會跳過它們——放著不解，連結就一直補不上。
    """
    index_names: set = set()
    derived_paths: list = []
    atoms: list = []
    for d in memory_dir_candidates(Path(cwd), root):
        for mine in d.rglob("*.mine"):
            original = mine.name[:-len(".mine")]
            if original in _SVN_INDEX_NAMES:
                index_names.add(original)
                if original in _DERIVED_INDEX_NAMES:
                    derived_paths.append(mine.with_name(original))
            elif original.endswith(".md"):
                atoms.append(original[:-3])
    out = []
    if index_names:
        if derived_paths and _derived_exit_hint is not None:
            msg = i18n.t("hook.session_start_vcs.svn_index_derived", names=', '.join(sorted(index_names)), hint=_derived_exit_hint('svn', derived_paths), cmd=_MERGE_CMD)
        else:
            msg = i18n.t("hook.session_start_vcs.svn_index_plain", names=', '.join(sorted(index_names)), cmd=_MERGE_CMD)
        out.append(msg)
    if atoms:
        shown = i18n.t("hook.session_start_vcs.list_sep").join(sorted(atoms)[:3]) + ("…" if len(atoms) > 3 else "")
        out.append(i18n.t("hook.session_start_vcs.svn_atom_conflict", n=len(atoms), shown=shown))
    return out


def _vcs_ignore_advisory(project_mem_dir) -> list:
    """專案記憶庫的衍生檔退出版控：設 svn:ignore／.gitignore＋「已被版控」告警一次（lib/vcs_ignore）。

    每個記憶庫每種 VCS 只處理一次（marker：workflow/vcs-ignore.json），之後零子行程；
    記憶庫目錄尚未版控時不記、下次再試。無 VCS／~/.claude 自己的記憶 → []。
    """
    try:
        if not project_mem_dir or _vcs_ignore_lines is None:
            return []
        return _vcs_ignore_lines(Path(project_mem_dir), WORKFLOW_DIR / "vcs-ignore.json", timeout=4.0)
    except Exception as e:  # noqa: BLE001
        _atom_debug_error("session_start:vcs_ignore_advisory", e)
        return [i18n.t("hook.session_start_vcs.vcs_ignore_failed")]


def _personal_sync_advisory(project_mem_dir, user: str) -> list:
    """本人 personal atom 的版控同步狀態 → 開場最多三行（無事零 context）。

    存在理由：personal 層的設計是「可上版控、僅本人可搜」（可見性由索引 scope=personal:<user>
    控管）。但索引三檔跟著 repo 走、personal 檔卻可能留在本機（沒 commit、或被 .gitignore 擋掉）
    → 他機索引懸空、兩機 hook 重建索引互相加回/拿掉。以前靠人傳話「請把 personal 上傳」；
    這裡讓每個人的 CC 在自己機器上看到自己的缺口，自己補。

    三種訊號（各自獨立、可同時出）：
      1. personal/<user>/ 被 .gitignore 擋住 → 提示移除該行
      2. 本人 personal 檔未 commit（untracked / modified）→ 提示收尾一起 commit
      3. 索引列了本人 personal atom 但本機無檔 → 多半留在本人另一台機器未 push

    唯讀 git；非 repo／無 user／git 不在 → []。自身出錯不阻斷 SessionStart。
    """
    try:
        if not project_mem_dir or not user:
            return []
        mem = Path(project_mem_dir)
        if not mem.is_dir():
            return []
        try:
            if mem.resolve() == Path(MEMORY_DIR).resolve():
                return []  # 全域核心 repo 對外公開發布，personal 依 .gitignore 留本機是刻意設計；只管專案層
        except OSError:
            pass
        personal_dir = mem / "personal" / user

        def _git(*args, timeout=5):
            return subprocess.run(
                ["git", "-C", str(mem), *args],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout,
                creationflags=no_window_flags(),
            )

        top = _git("rev-parse", "--show-toplevel")
        if top.returncode != 0:  # 不是 repo → 不吵
            return []
        try:
            rel = personal_dir.resolve().relative_to(Path(top.stdout.strip()).resolve()).as_posix()
        except Exception:
            rel = personal_dir.as_posix()

        # ── 3. 索引懸空（先算：決定本機無目錄時要不要繼續）──
        dangling: list = []
        idx = mem / "_atom_index.json"
        if idx.exists():
            try:
                atoms = json.loads(idx.read_text(encoding="utf-8")).get("atoms", [])
                prefix = f"memory/personal/{user}/"
                for a in atoms:
                    ap = str(a.get("path", ""))
                    if ap.startswith(prefix) and not (mem.parent / ap).exists():
                        dangling.append(a.get("name") or Path(ap).stem)
            except Exception as e:  # noqa: BLE001
                _atom_debug_error("session_start:personal_sync_index", e)

        if not personal_dir.is_dir() and not dangling:
            return []

        out: list = []

        # ── 1. 被 .gitignore 擋住 ──
        # --no-index：不受目錄內已追蹤檔干擾；探測目錄內虛擬檔名（對目錄本身判定不穩）
        ign = _git("check-ignore", "-q", "--no-index", "--", str(personal_dir / "_probe.md"))
        if ign.returncode == 0:
            out.append(i18n.t("hook.session_start_vcs.personal_ignored", rel=rel))

        # ── 2. 未 commit ──
        if personal_dir.is_dir() and ign.returncode != 0:
            st = _git("status", "--porcelain=v1", "--untracked-files=all", "--", str(personal_dir))
            pending = []
            for line in (st.stdout or "").splitlines():
                if len(line) < 4:
                    continue
                path = line[3:].strip().strip('"')
                if " -> " in path:
                    path = path.split(" -> ", 1)[1]
                if path.endswith(".access.json"):
                    continue
                pending.append(Path(path).stem)
            if pending:
                shown = ", ".join(pending[:3]) + ("…" if len(pending) > 3 else "")
                out.append(i18n.t("hook.session_start_vcs.personal_pending", n=len(pending), shown=shown, rel=rel))

        if dangling:
            shown = ", ".join(dangling[:3]) + ("…" if len(dangling) > 3 else "")
            out.append(i18n.t("hook.session_start_vcs.personal_dangling", n=len(dangling), shown=shown, rel=rel))
        return out
    except Exception as e:  # noqa: BLE001
        _atom_debug_error("session_start:personal_sync_advisory", e)
        return [i18n.t("hook.session_start_vcs.personal_check_failed", etype=type(e).__name__)]
