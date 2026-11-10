"""
handlers/session_start_index.py — SessionStart 的索引自癒與 MEMORY.md 重生

做什麼：開場時索引檔不在或比記憶卡舊就就地重建（索引是衍生檔，能從 atom 重生）、
根層個人卡登記還沒分流就跑一次分流；依角色動態重寫專案的 MEMORY.md。
誰叫它：handlers/session_start.py 的 handle_session_start（讀索引前、候選池建好後）。
"""

import json
import os
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from wg_core import CODE_DIR, MEMORY_DIR, MEMORY_INDEX, _atom_debug_error
from wg_platform import no_window_flags, paste_command
from atom_index_json import has_indexable_cards   # lib/：磁碟上有沒有該登記的卡
import i18n   # lib/：給人看的提示走語言表
from handlers._shared import _MEMORY_MD_AUTO_HEADER
from handlers.session_start_vcs import _DERIVED_DIR_NAMES, _rebuild_manual, _under_claude_dir   # 衍生檔名單與退出版控判斷的單一出處


def _index_stale_reason(memory_dir: Path, json_path: Path) -> str:
    """索引在，但磁碟上的記憶卡已經變了 → 回一句原因；沒變回空字串。

    索引不入版控之後，隊友的卡經 svn／git 更新進來，本機那份舊索引不會跟著動，
    新卡就不會被注入。這裡只看兩件事：有沒有記憶卡比索引新、索引登記的檔還在不在。
    索引讀不到就當沒變——判斷失敗不該變成每次都重建。
    """
    try:
        index_mtime = json_path.stat().st_mtime
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    for p in memory_dir.rglob("*.md"):
        rel = p.relative_to(memory_dir)
        if rel.parts[0] in _DERIVED_DIR_NAMES or p.name.startswith("_") or p.name == "MEMORY.md":
            continue
        try:
            if p.stat().st_mtime > index_mtime:
                return i18n.t("hook.session_start_index.newer_than_index", rel=rel.as_posix())
        except OSError:
            continue
    for a in data.get("atoms") or []:
        path = a.get("path") or ""
        if path and not (memory_dir.parent / path).exists():
            return i18n.t("hook.session_start_index.registered_missing", path=path)
    return ""


def _split_personal_if_pending(memory_dir: Path) -> str:
    """根層個人卡登記還沒搬出共用索引，或 pull 來的個人卡還沒登記 → 跑一次分流，回要顯示的一行。
    結果看分流後的狀態，不看 exit code：工具遇到別的索引漂移也會回 1。"""
    try:
        from atom_index_json import personal_split_pending
        if not personal_split_pending(memory_dir):
            return ""
        import subprocess as _sp
        r = _sp.run([sys.executable, str(CODE_DIR / "tools" / "sync-atom-index.py"),
                     "--memory-dir", str(memory_dir), "--split-personal"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                    creationflags=no_window_flags())
        if personal_split_pending(memory_dir):
            return i18n.t("hook.session_start_index.split_not_moved", err=(r.stderr or '').strip()[:200],
                          cmd=paste_command(CODE_DIR / "tools" / "sync-atom-index.py"))
    except Exception as e:  # noqa: BLE001
        _atom_debug_error("session_start:split_personal", e)
        return i18n.t("hook.session_start_index.split_failed", err=e, cmd=paste_command(CODE_DIR / "tools" / "sync-atom-index.py"))
    return i18n.t("hook.session_start_index.split_done")


def _sync_shipped_cards(memory_dir: Optional[Path]) -> Tuple[str, bool]:
    """插件版、資料根的記憶夾：展開／更新出廠卡（lib/shipped_cards.sync_shipped）。回 (要顯示的一行, 卡片檔有沒有增刪改)。
    安裝版（程式碼根＝資料根，卡片就在 repo 裡）與專案記憶夾不做。出廠包壞了要大聲：出廠知識這場整批送不到。"""
    if memory_dir is None or memory_dir != MEMORY_DIR:
        return "", False
    from edition import edition, PLUGIN   # lib/
    if edition() != PLUGIN:
        return "", False
    try:
        from shipped_cards import sync_shipped   # lib/
        st = sync_shipped(CODE_DIR, memory_dir.parent)
    except Exception as e:  # noqa: BLE001
        _atom_debug_error("session_start:shipped_cards", e)
        return i18n.t("hook.session_start_index.shipped_failed", err=str(e)[:200]), False
    if st["skipped"]:
        return "", False
    lines = []
    if st["fresh"] or st["updated"] or st["removed"] or st["docs"]:
        lines.append(i18n.t("hook.session_start_index.shipped_synced", version=st["version"], fresh=st["fresh"],
                            updated=st["updated"], removed=st["removed"], secs=st["secs"]))
    if st["kept_user"]:
        lines.append(i18n.t("hook.session_start_index.shipped_kept_user", n=st["kept_user"],
                            names=", ".join(st["kept_names"][:5])))
    return " ".join(lines), bool(st["changed"])


def _rebuild_index_if_missing(memory_dir: Optional[Path]) -> str:
    """插件版先展開／更新出廠卡；卡片有變就強制重建索引與目錄，否則照下面的缺檔／過期判斷。"""
    shipped_line, shipped_changed = _sync_shipped_cards(memory_dir)
    note = _rebuild_index(memory_dir, force=i18n.t("hook.session_start_index.what_shipped") if shipped_changed else "")
    return " ".join(x for x in (shipped_line, note) if x)


def _rebuild_index(memory_dir: Optional[Path], force: str = "") -> str:
    """索引檔不在（沒入版控／被清掉／新 clone）、或專案記憶卡比索引新 → 就地從磁碟重建。

    索引是衍生檔，本來就該能從 atom 重生；能重生才敢不入版控，
    不入版控才不會「兩個人各加一顆 atom ＝ 同一區塊各插一列 ＝ 衝突」。
    專案記憶的索引不入版控，所以隊友更新進來的卡要靠這裡的過期檢查才進得了索引；
    ~/.claude 自己的記憶索引在版控裡、隨 pull 更新，不做過期檢查。
    _ATOM_INDEX.md（mirror）缺檔同理：JSON 在就直接重生、不起子行程。
    回傳要顯示的一行（沒事做回空字串）。重建失敗要大聲並附手動指令——
    否則 trigger 注入靜默變空，沒人知道。
    """
    if not memory_dir or not memory_dir.is_dir():
        return ""
    json_path = memory_dir / "_atom_index.json"
    md_path = memory_dir / "_ATOM_INDEX.md"
    manual = _rebuild_manual(memory_dir)
    stale = force   # 出廠卡剛增刪改（force 是原因）：跳過下面的判斷直接重建，再重生 MEMORY.md 與各層 _INDEX.md
    if force:
        pass
    elif json_path.exists() and md_path.exists():
        if _under_claude_dir(memory_dir):
            return _split_personal_if_pending(memory_dir)
        stale = _index_stale_reason(memory_dir, json_path)
        if not stale:
            return ""
    elif json_path.exists():
        try:
            from atom_index_json import regenerate_atom_index_md
            regenerate_atom_index_md(memory_dir)
        except Exception as e:  # noqa: BLE001
            _atom_debug_error("session_start:regenerate_mirror", e)
            return i18n.t("hook.session_start_index.regen_failed", err=e, manual=manual)
        if not md_path.exists():
            return i18n.t("hook.session_start_index.regen_no_output", manual=manual)
        return i18n.t("hook.session_start_index.regen_done", dir=memory_dir)
    elif not md_path.exists() and not has_indexable_cards(memory_dir):
        return ""   # 一張卡都還沒有（新安裝只有範本 MEMORY.md）：沒東西可重建，不生空索引（生了會被背景同步當成變更提交）
    what = i18n.t("hook.session_start_index.what_stale", stale=stale) if stale else i18n.t("hook.session_start_index.what_missing")
    harm = i18n.t("hook.session_start_index.harm_stale") if stale else i18n.t("hook.session_start_index.harm_missing")
    import subprocess as _sp
    cmd = [sys.executable, str(CODE_DIR / "tools" / "sync-atom-index.py"),
           "--memory-dir", str(memory_dir), "--rebuild"]
    try:
        r = _sp.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                    creationflags=no_window_flags())
    except (OSError, _sp.SubprocessError) as e:
        return i18n.t("hook.session_start_index.rebuild_exc", what=what, err=e, harm=harm, manual=manual)
    if not json_path.exists():
        return i18n.t("hook.session_start_index.rebuild_no_output", err=(r.stderr or '').strip()[:200], manual=manual)
    if stale:
        if r.returncode != 0:
            return i18n.t("hook.session_start_index.rebuild_failed", what=what, err=(r.stderr or '').strip()[:200], harm=harm, manual=manual)
        # 內容沒變時重建工具可能不改寫檔案：碰一下時間，否則同一張卡每次開 session 都判過期
        try:
            os.utime(json_path)
        except OSError:
            pass
    # 帶上條目數：從零重建只收「磁碟掃得到的 atom」，舊索引若登記過非 atom 檔
    #（_reference/ 下的參考文、沒有元資料的敘事文），重建後條目會變少。
    # 不報數字的話那個落差是隱形的。
    try:
        import json as _json
        n = len(_json.loads(json_path.read_text(encoding="utf-8")).get("atoms") or [])
        count = i18n.t("hook.session_start_index.count", n=n)
    except (OSError, ValueError):
        count = ""
    mirror = "" if md_path.exists() else i18n.t("hook.session_start_index.mirror_missing", manual=manual)
    if force:
        try:
            r = _sp.run([sys.executable, str(CODE_DIR / "tools" / "sync-memory-index.py"), "--write",
                         "--memory-dir", str(memory_dir)], capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=120, creationflags=no_window_flags())
            if r.returncode != 0:
                mirror += i18n.t("hook.session_start_index.catalog_failed", err=(r.stderr or '').strip()[:200])
        except (OSError, _sp.SubprocessError) as e:
            mirror += i18n.t("hook.session_start_index.catalog_failed", err=e)
    return i18n.t("hook.session_start_index.rebuilt", what=what, dir=memory_dir, count=count, mirror=mirror)


def _regenerate_role_filtered_memory_index(
    project_mem_dir: Path, user: str, roles: List[str], management: bool,
    v4_entries: List[Tuple[str, str, List[str]]],
) -> None:
    """V4：依角色動態寫 {proj}/.claude/memory/MEMORY.md（SPEC §3）。"""
    target = project_mem_dir / MEMORY_INDEX
    if target.exists():
        try:
            first = target.read_text(encoding="utf-8-sig").split("\n", 1)[0].strip()
        except (OSError, UnicodeDecodeError):
            first = ""
        if first != _MEMORY_MD_AUTO_HEADER:
            return

    lines = [
        _MEMORY_MD_AUTO_HEADER,
        f"# MEMORY Index — {user} ({', '.join(roles) or 'programmer'})",
        "",
        "> Generated by workflow-guardian SessionStart. Filtered by role.",   # 寫進檔案的字固定英文，不走語言表（多機同步才不會互相洗成不同語言）
        f"> User: {user} | Roles: {', '.join(roles) or 'programmer'} | Management: {management}",
        "",
        "| Atom | Path | Trigger | Scope |",
        "|------|------|---------|-------|",
    ]
    for name, rel, triggers in sorted(v4_entries, key=lambda e: e[0]):
        parts = Path(rel).parts
        scope = ""
        try:
            subscope = parts[1]
            if subscope == "shared":
                scope = "shared"
            elif subscope == "roles" and len(parts) >= 4:
                scope = f"role:{parts[2]}"
            elif subscope == "personal" and len(parts) >= 4:
                scope = f"personal:{parts[2]}"
        except IndexError:
            pass
        trig_str = ", ".join(triggers) if triggers else ""
        lines.append(f"| {name} | {rel} | {trig_str} | {scope} |")
    try:
        target.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    except OSError as e:
        _atom_debug_error("regenerate_memory_md", e)
