"""
handlers/session_start.py — SessionStart hook handler（事件入口與流程）

handle_session_start 是目錄：一行一步，步驟函式照呼叫順序排在它下面（每段結束都有 _hook_lap 計時，超時降級送出已備好的行）：
前置（專案根宣告、缺詞表紅字與核心規則、log 輪替、同 session 去重）→ 沿用舊 state（compact／resume）或新開場
（拉取同步、索引自癒、候選池、索引校驗、健檢與向量旗標、各種提示）→ 標記檔與回顧提示 → 服務檢查與哨兵 → 寫 state 輸出
→ 背景起向量服務。步驟之間的狀態走參數與回傳：開場行 lines 與 state 明著傳、就地加改，後段要用的值由前段回傳。
步驟函式留在本檔、不拆出去：它們用的名字（read_state、WORKFLOW_DIR、REALM_AUTOMOVE_MARKER…）測試在本模組上換。

各件事的實作在同資料夾：
  session_start_index.py      索引缺檔／過期重建、角色過濾 MEMORY.md
  session_start_vcs.py        記憶庫版控同步、未 push、索引衝突、退出版控、personal 卡缺口
  session_start_vector.py     向量服務旗標與提示
  session_start_sentinels.py  IDENTITY／必載檔哨兵、SessionEnd 殘留、週健檢、回訪
  session_start_project.py    專案根、組織層、_AIDocs／本地範疇指標、scope 整理提示
  session_start_state.py      compact／resume 的 state 沿用與重建、接續指標、git 快照
下列 import 同時是對外名字：測試與其他 hook 以 handlers.session_start.<名> 取用，入口保留。
"""

import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from wg_core import (
    CLAUDE_DIR, CODE_DIR, WORKFLOW_DIR, MEMORY_DIR,
    MEMORY_INDEX,
    _now_iso, _atom_debug_error, v6_enabled, guard_family_on, cap_hook_output,
    get_project_memory_dir, find_project_root,
    register_project,
    read_state, write_state, new_state, _find_active_sibling_state,
    _check_mcp_servers,
    iter_realm_category_dirs,
    REALM_AUTOMOVE_MARKER,
    resolve_project_root, org_memory_root,
    CONFIG_PATH, degraded_notify,
)
from wg_platform import background_spawn_kwargs  # noqa: E402
from wg_atoms import (
    parse_memory_index, parse_aidocs_index, extract_aidocs_keywords,
    build_candidate_pool,
)
from atom_spec import SCOPE_DIR_NAMES   # lib/：共用名字
from atom_index_json import has_indexable_cards   # lib/：索引讀成 0 筆時分「還沒有卡」與「索引壞了」
from atom_taxonomy import TaxonomyUnavailable, load_taxonomy   # lib/（wg_core 已放進 sys.path）
from degraded import session_lines as degraded_session_lines   # lib/：開場紅字的單一出處
from edition import config_layers   # lib/：哪幾層設定檔算數（插件版出廠那層也算）
from wg_evasion import (
    _load_oscillation_warnings, _check_periodic_review_due,
)
from wg_roles import (
    get_current_user, load_user_role, is_management, bootstrap_personal_dir,
)
from handlers._shared import (
    _call_project_hook, _cleanup_old_states, prune_workflow_records,
)
import wg_hook_timing as _hook_timing
import wg_plugins
import i18n   # lib/：給人看的提示走語言表
from wg_hook_timing import lap as _hook_lap
from handlers.session_start_sentinels import (   # noqa: F401
    check_identity_integrity, check_always_load_contracts, _check_se_sentinel_residual,
    HEALTH_RUN_STALE_DAYS, _health_advisory, _followup_advisory,
)
from handlers.session_start_vector import _vector_package_notice   # noqa: E402
from handlers.session_start_vector import (   # noqa: F401
    _refresh_vector_flag, _vector_index_advisory, _notify_vector_down,
)
from handlers.session_start_vcs import (   # noqa: F401
    _spawn_pull_sync, _maybe_spawn_provenance_backfill, _unpushed_advisory,
    _index_conflict_advisory, _svn_index_conflict_advisory, _vcs_ignore_advisory,
    _personal_sync_advisory,
)
from handlers.session_start_index import (   # noqa: F401
    _split_personal_if_pending, _rebuild_index_if_missing, _regenerate_role_filtered_memory_index,
)
from handlers.session_start_project import (   # noqa: F401
    _scope_layout_advisory, _aidocs_opening_lines, _local_catalog_lines, _org_advisory,
    _project_memory_nudge, _project_root_notice, _first_session_hint_lines,
)
from handlers.session_start_state import (   # noqa: F401
    _root_fingerprint_matches, _CARRY_ON_REBUILD, floor_turn_seq, carry_on_rebuild,
    _next_phase_pointer, _take_git_snapshot, _compact_recovery, _resume_cache_note,
)

# Ollama client 在 tools/ 下（dispatcher 已加 sys.path）；用本檔位置推樹根，不用 Path.home()
_TOOLS_DIR = str(Path(__file__).resolve().parents[2] / "tools")
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
try:
    from ollama_client import check_long_die_status
except ImportError:
    check_long_die_status = lambda: None  # noqa: E731

def _degraded_opening_lines(config: Dict[str, Any]) -> List[str]:
    """開場紅字：逼還沒跑過的載入點回報（realm 與退避詞表在模組載入時已自己回報），收成每個功能一行。"""
    if not any(p.exists() for p in config_layers(CONFIG_PATH, with_local=False)):   # 插件版：出廠那層在就不算缺
        degraded_notify("config", reason="file missing")
    elif config.get("_config_parse_failed"):
        degraded_notify("config", reason="JSON parse failed")
    org_memory_root()
    try:
        load_taxonomy()
    except TaxonomyUnavailable:
        pass
    _vector_package_notice(config)
    _mcp_unavailable_notice()
    return degraded_session_lines()


def _mcp_unavailable_notice() -> None:
    """MCP 工具用不了的原因與下一步：hooks/ensure-mcp.py 註冊不了時留的旗標檔（沒有 Node.js、找不到 claude 或註冊失敗），
    內容就是給人看的那段（語言表的字）→ 開場紅字。.claude.json 已登記 workflow-guardian 時，註冊失敗那份是舊的，不報。
    ensure-mcp 是同時跑的另一支 hook：這一場它剛寫的旗標可能下一場才讀到（安裝器結尾與 --verify 當下就會講）。"""
    for name, stale_once_registered in (("mcp-needs-node.flag", False), ("mcp-cli-failed.flag", True)):
        try:
            reason = (WORKFLOW_DIR / name).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if reason and not (stale_once_registered and _mcp_registered()):
            degraded_notify("mcp", reason=reason)
            return


def _mcp_registered() -> bool:
    """Claude Code 的 .claude.json（CLAUDE_CONFIG_DIR 或家目錄底下）登記了 workflow-guardian 這個 MCP 沒。"""
    from project_root import claude_global_config   # lib/
    try:
        servers = json.loads(claude_global_config().read_text(encoding="utf-8")).get("mcpServers") or {}
    except (OSError, ValueError, AttributeError):
        return False
    return "workflow-guardian" in servers


def _core_rule_lines(source: str) -> List[str]:
    """核心的寫卡規則（lib/i18n/core-rules.<語言>.md，跟語言設定走）。resume 接著舊對話、規則還在 context 裡，不重送。"""
    if source == "resume":
        return []
    from i18n import current_language   # lib/
    path = CODE_DIR / "lib" / "i18n" / f"core-rules.{current_language()}.md"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        degraded_notify("core_rules", path=path)
        return []
    return [text.replace("{code_root}", CODE_DIR.as_posix())]


def _count_pending_review(project_mem_dir: Optional[Path]) -> int:
    if not project_mem_dir:
        return 0
    pr = project_mem_dir / "shared" / "_pending_review"
    if not pr.is_dir():
        return 0
    try:
        return sum(1 for p in pr.glob("*.md"))
    except OSError:
        return 0


def _count_recent_auto_atoms(user: str, cwd: str, hours: int = 24) -> int:
    """Count auto-extracted-v4.1 atoms created within last N hours."""
    import re as _re
    count = 0
    cutoff = datetime.now() - timedelta(hours=hours)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    dirs_to_scan: List[Path] = []
    project_root = find_project_root(cwd)
    if project_root:
        d = Path(project_root) / ".claude" / "memory" / "personal" / "auto" / user
        if d.is_dir():
            dirs_to_scan.append(d)
    d = CLAUDE_DIR / "memory" / "personal" / "auto" / user
    if d.is_dir() and d not in dirs_to_scan:
        dirs_to_scan.append(d)

    for auto_dir in dirs_to_scan:
        for md_file in auto_dir.glob("*.md"):
            if md_file.name.startswith("_"):
                continue
            try:
                text = md_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if "auto-extracted-v4.1" not in text:
                continue
            m = _re.search(r'^-\s*Created:\s*(\S+)', text, _re.MULTILINE)
            if m:
                if m.group(1) >= cutoff_str:
                    count += 1
            else:
                try:
                    mtime = md_file.stat().st_mtime
                    if mtime >= cutoff.timestamp():
                        count += 1
                except OSError:
                    pass
    return count


def _ss_output(lines: List[str], system_message: str = "") -> str:
    out: Dict[str, Any] = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n".join(lines)}}
    if system_message:   # 給使用者看的那一行（目前只有 RESUME_CACHE_REWRITE_NOTE）
        out["systemMessage"] = system_message
    return json.dumps(cap_hook_output(out), ensure_ascii=False)


def _fallback_output(lines: List[str]) -> str:
    """背景鬧鐘代送（到上限 95% 還沒送出）：當下已備好的開場提示＋一行說明；state 這次沒寫回。"""
    return _ss_output([*lines, i18n.t("hook.session_start.timing_fallback")])


def _degraded_exit(session_id: str, state: Dict[str, Any], lines: List[str]) -> None:
    """開場超時降級（累計過上限 80%）：state 寫回、已備好的提示照送，後面的選配提示與檢查
    這次跳過（含開場 git 快照、清舊 state、向量服務旗標與啟動）。"""
    lines.append(_hook_timing.degrade_note())
    write_state(session_id, state)
    _hook_timing.claim_output()
    print(_ss_output(lines))
    sys.exit(0)


def _lap_or_degrade(segment: str, session_id: str, state: Dict[str, Any], lines: List[str]) -> None:
    """一段跑完打一點；累計過上限 80% 就走 _degraded_exit（state 寫回、已備好的行送出、結束，後面的段落這次不跑）。"""
    if _hook_lap(segment):
        _degraded_exit(session_id, state, lines)


def handle_session_start(input_data: Dict[str, Any], config: Dict[str, Any]) -> None:
    """SessionStart 入口，讀起來是目錄：一行一步，步驟函式照呼叫順序排在下面。
    步驟之間的狀態都走參數與回傳：lines（開場行）與 state 明著傳、就地加改，後段要用的值由前段回傳。
    _lap_or_degrade 是超時降級點：那一段跑完打一點，累計過上限 80% 就寫回 state、送出已備好的行、結束。"""
    session_id = input_data.get("session_id", "unknown")
    cwd = input_data.get("cwd", "")
    source = input_data.get("source", "startup")
    v6 = v6_enabled(config)     # 關＝只送 9e476fd0 就有的開場注入與提示
    guard = guard_family_on(config)   # 關（插件版）＝不拍 git 快照（殘檔帳本用，收尾檢核不出廠）

    # ── 前置：開場一定送的行、log 輪替、同 session 去重、讀舊 state
    root_res, root_lines = _opening_root_lines(cwd, source, config, v6 and guard)   # 首次開場提示（OverviewHub 骨架）屬守門家族
    _rotate_logs(session_id, v6)
    sibling = _dedup_sibling_session(session_id, cwd, source, root_res, root_lines, config)
    existing = read_state(session_id)
    # 壓縮後送回的卡片內文：在兩條路（沿用舊 state／專案根變了重建）分岔前從舊 state 組好，匯合後才送
    compact_blob, compact_sent, compact_unresolved = (
        _compact_recovery(existing, config) if source == "compact" else ("", [], []))
    _hook_lap("前置")

    # ── compact／resume 接續：專案根指紋沒變就沿用舊 state；其餘（新 session、指紋變了）走新開場
    root_rebuilt = False
    if existing and source in ("compact", "resume") and not _root_fingerprint_matches(existing, root_res):
        # 專案根指紋變了（cwd 換了、宣告檔改了、或舊 state 還沒有指紋）→ 走新開場那條重算
        # atom_index / aidocs，session 脈絡（修改檔、知識佇列…）照搬
        root_rebuilt = True
    if existing and source in ("compact", "resume") and not root_rebuilt:
        state = existing
        lines = _reuse_old_state(state, source, cwd, root_lines, compact_blob)
        vector_flag: Optional[str] = None   # 這條沒有健檢段，收尾再 ping 向量服務
    else:
        state, lines, vector_flag = _open_new_session(
            input_data, config, session_id, cwd, source, root_res, root_lines, existing, root_rebuilt, sibling)

    # ── 開場主段收尾：兩條路匯合
    _reinject_compact_cards(lines, state, compact_blob, compact_sent, compact_unresolved)
    resume_note = _resume_cache_note(input_data, source)   # 規則 RESUME_CACHE_REWRITE_NOTE：降級提早送出時也在 additionalContext 裡
    if resume_note:
        lines.append(resume_note)
    _hook_timing.set_fallback(lambda: _fallback_output(lines))   # compact／resume 那支也有代送（startup 前面已登記）
    _lap_or_degrade("開場主段", session_id, state, lines)

    # ── 匯合後的提示與檢查
    _marker_file_notices(lines, cwd)
    _lap_or_degrade("標記檔提示", session_id, state, lines)
    _review_notices(lines, input_data, config, session_id, source, state)
    _lap_or_degrade("回顧提示", session_id, state, lines)
    _service_notices(lines)
    _lap_or_degrade("服務檢查", session_id, state, lines)
    _contract_sentinels(lines, v6)
    _lap_or_degrade("必載檔哨兵", session_id, state, lines)
    if v6 and guard:
        _take_git_snapshot(state, cwd)
    _lap_or_degrade("git 快照", session_id, state, lines)

    # ── 寫 state、清舊 state 與向量旗標、輸出、背景起向量服務
    write_state(session_id, state)
    _cleanup_and_refresh_vector_flag(config, vector_flag, lines)
    _hook_lap("清舊 state 與向量旗標")
    _hook_timing.claim_output()
    print(_ss_output(lines, resume_note))
    _spawn_vector_starter(config, state, session_id)
    sys.exit(0)


# ─── 前置 ────────────────────────────────────────────────────────────────────


def _opening_root_lines(cwd: str, source: str, config: Dict[str, Any], v6: bool) -> Tuple[Any, List[str]]:
    """開場一定送的行：專案根宣告（寫碼守門開時加首次開場提示）、缺詞表／缺設定的紅字、核心寫卡規則。
    回 (專案根解析結果，沒有或解析出錯是 None；這幾行)。"""
    root_res = None
    root_lines: List[str] = []
    try:
        root_res = resolve_project_root(cwd) if (resolve_project_root and cwd) else None
        root_lines = _project_root_notice(root_res, cwd)
    except Exception as e:
        _atom_debug_error("session_start:project_root", e)
    if v6 and guard_family_on(config):   # 部位表骨架提示（OverviewHub，守門家族）：插件版不出廠 lib/overview_scaffold.py
        root_lines = [*root_lines, *_first_session_hint_lines(root_res, cwd)]
    # root_lines 三條開場路（合併、恢復、新開）與超時降級都送得到：缺詞表／缺設定的紅字與核心規則放這裡
    try:
        core_rule_lines = _core_rule_lines(source)
        root_lines = [*_degraded_opening_lines(config), *core_rule_lines, *root_lines]
    except Exception as e:
        _atom_debug_error("session_start:opening_lines", e)
    return root_res, root_lines


def _rotate_logs(session_id: str, v6: bool) -> None:
    """log 輪替，防 log 無限長；寫碼守門開時另把 workflow/ 執行紀錄照 _shared.WORKFLOW_RETENTION 清掉。"""
    try:
        from wg_core import rotate_log_if_oversized
        rotate_log_if_oversized(WORKFLOW_DIR / "guardian-crash.log", max_mb=10)
        rotate_log_if_oversized(WORKFLOW_DIR / "extract-worker.log", max_mb=10)
        if v6:
            prune_workflow_records(current_sid=session_id)  # workflow/ 執行紀錄照 _shared.WORKFLOW_RETENTION 清
    except Exception as e:
        _atom_debug_error("session_start:log_rotation", e)


def _dedup_sibling_session(session_id: str, cwd: str, source: str, root_res: Any,
                           root_lines: List[str], config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """SessionStart 去重（V3/1.5A）：同 cwd 有近期活著的另一個 session。resume 撞上 → 寫一份 merged state 指過去、
    送出（開場紅字一樣要有，向量服務的也是）、結束；其他來源回找到的那份（startup 拿它跳過向量服務啟動）。
    compact 是同一個 session，不查、回 None。"""
    if source == "compact":
        return None
    sibling = _find_active_sibling_state(cwd, session_id)
    if not sibling or source != "resume":
        return sibling
    redirect_state = new_state(session_id, cwd, source)
    redirect_state["merged_into"] = sibling["session"]["id"]
    redirect_state["phase"] = "merged"
    if root_res is not None:
        redirect_state["atom_index"] = {
            "project_root": str(root_res.path) if root_res.path else "",
            "project_root_fingerprint": root_res.fingerprint,
        }
    write_state(session_id, redirect_state)
    lines = [f"[Workflow Guardian] Session merged ({source}).", *root_lines]
    _vector_flag_lines(lines, config)
    lines.extend(degraded_session_lines())
    print(json.dumps(cap_hook_output({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n".join(lines),
        }
    }), ensure_ascii=False))
    sys.exit(0)


# ─── compact／resume 沿用舊 state ─────────────────────────────────────────────


def _reuse_old_state(state: Dict[str, Any], source: str, cwd: str, root_lines: List[str],
                     compact_blob: str) -> List[str]:
    """compact／resume 複用舊 state 的 atom_index 快取（指紋相同才走到這裡）：回合編號墊底、清空本輪已注入名單，
    回開場行（恢復摘要、接續指標、最近改的檔、待收的知識、壓縮前已載入的卡名）。"""
    floor_turn_seq(state)
    prev_atoms = state.get("injected_atoms", [])
    state["injected_atoms"] = []
    mod_count = len(state.get("modified_files", []))
    kq_count = len(state.get("knowledge_queue", []))
    phase = state.get("phase", "working")
    lines = [
        f"[Workflow Guardian] Session resumed ({source}). Phase: {phase}.",
        f"Modified files: {mod_count}. Knowledge queue: {kq_count}.",
        *root_lines,
        *_next_phase_pointer(cwd, source),
    ]
    if mod_count > 0:
        files = [m["path"].rsplit("/", 1)[-1] for m in state["modified_files"][-5:]]
        lines.append(f"Recent: {', '.join(files)}")
    if kq_count > 0:
        items = [q["content"][:40] for q in state["knowledge_queue"][:3]]
        lines.append(f"Pending knowledge: {'; '.join(items)}")
    # 壓縮後送得出卡片內文的話卡名已在內文裡；resume 或組不出內文才只列卡名
    if prev_atoms and not compact_blob:
        atom_names = ", ".join(prev_atoms)
        lines.append(i18n.t("hook.session_start.atom_recovery", names=atom_names))
    return lines


# ─── 新開場 ──────────────────────────────────────────────────────────────────


def _open_new_session(input_data: Dict[str, Any], config: Dict[str, Any], session_id: str, cwd: str, source: str,
                      root_res: Any, root_lines: List[str], existing: Optional[Dict[str, Any]], root_rebuilt: bool,
                      sibling: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[str], Optional[str]]:
    """新開場，或 compact／resume 時專案根指紋變了（重建，session 脈絡照搬）：新 state、索引自癒、候選池、索引校驗，
    再跑選配提示。回 (state, 開場行, 健檢段 ping 向量服務的結果)。
    背景鬧鐘代送讀的是這個函式的 lines 變數：候選池那段把它換成以摘要開頭的新 list，代送跟著換。"""
    state = new_state(session_id, cwd, source)
    if root_rebuilt and existing:
        carry_on_rebuild(existing, state)
    if sibling and source == "startup":
        state["_skip_vector_init"] = True

    # 開場超時降級、或背景鬧鐘代送時，至少送出這幾行
    lines = ["[Workflow Guardian] Active.", *root_lines]
    _hook_timing.set_fallback(lambda: _fallback_output(lines))

    # ── 索引與職能
    # 記憶層背景「拉」：在首次讀索引之前 detached 起 vcs-sync worker（秒回、不等）。
    # 趕不趕得上本次候選池隨緣——拉入的 atom 下一個 session 才一定進候選池，advisory 只報同步事實。
    _spawn_pull_sync(session_id, cwd, config)

    _index_rebuild_notes: List[str] = []   # 索引自癒：缺檔／過期就地重建，各回一行（沒事回空字串）
    _index_rebuild_notes.append(_rebuild_index_if_missing(MEMORY_DIR))
    global_atoms = parse_memory_index(MEMORY_DIR)
    project_mem_dir = get_project_memory_dir(cwd)
    _index_rebuild_notes.append(_rebuild_index_if_missing(project_mem_dir))
    project_root = find_project_root(cwd)

    register_project(cwd)
    nudge_lines: List[str] = []   # 候選池段會換掉 lines，提示先放這，換完再併進最終那份
    _nudge_project_memory_layer(nudge_lines, state, cwd, project_root, project_mem_dir)
    v4_user, v4_roles, v4_mgmt = _user_identity(cwd)
    _hook_lap("索引與職能")

    # ── 候選池：記進 state；開場行換成以摘要開頭的新 list（提示行併進新 list）
    org_root, pool, global_atoms, aidocs_entries = _candidate_pool(
        state, cwd, root_res, root_rebuilt, project_mem_dir, project_root, v4_user, v4_roles, v4_mgmt, global_atoms)
    g_names = [n for n, _, _ in global_atoms]
    p_names = [n for n, _, _ in pool["project"]]
    lines = [
        "[Workflow Guardian] Active." if not root_rebuilt
        else i18n.t("hook.session_start.resumed_root_rebuilt", source=source),
        f"Global: {len(g_names)} atoms. Project: {len(p_names)}.",
        *[n for n in _index_rebuild_notes if n],
        *root_lines,
        *nudge_lines,
    ]
    _lap_or_degrade("候選池", session_id, state, lines)

    # ── 索引校驗
    _global_index_zero_check(lines, g_names)
    _index_disk_check(lines, global_atoms)
    _skill_index_check(lines, config)
    _lap_or_degrade("索引校驗", session_id, state, lines)

    # 選配提示：每段跑完打一點，累計過上限 80% 就送出收掉，後面的這次不跑。

    # ── 健檢：週健檢死人開關、插件點、向量服務旗標、這段新冒出的開場紅字
    # tools/health-weekly.py（Task Scheduler 每週跑）落 health-last-run.json。
    # 缺檔/逾期 = 排程器本身死了；red>0 = 上次健檢有待處理項。兩者都必須
    # 在 session 內浮出（fail-open 必告知）——「靜默死 27 天」的最後防線。
    lines.extend(_health_advisory(WORKFLOW_DIR / "health-last-run.json"))
    lines.extend(wg_plugins.emit("SessionStart.new_session", state=state, config=config, session_id=session_id,
                                 input_data=input_data, source=source))
    vector_flag = _vector_flag_lines(lines, config)
    lines.extend(degraded_session_lines())
    _lap_or_degrade("健檢", session_id, state, lines)

    # ── 版控同步
    # 未推送 commit：SessionEnd 晉升自動提交的 push 走背景、失敗當下無人知 → 這裡補可見性。
    lines.extend(_unpushed_advisory())
    lines.extend(_index_conflict_advisory(cwd))
    _lap_or_degrade("未推送與索引衝突", session_id, state, lines)
    lines.extend(_followup_advisory())
    _lap_or_degrade("回訪", session_id, state, lines)
    lines.extend(_scope_layout_advisory(project_mem_dir))
    lines.extend(_vcs_ignore_advisory(project_mem_dir))
    lines.extend(_personal_sync_advisory(project_mem_dir, v4_user))
    _lap_or_degrade("分層與同步", session_id, state, lines)

    # ── 組織層
    lines.extend(_org_advisory(org_root, pool))
    _maybe_spawn_provenance_backfill(config)
    _lap_or_degrade("組織層", session_id, state, lines)

    # ── 身分與專案提示（沒有自己的計時點，算進開場主段）
    _role_lines(lines, v4_user, v4_roles, v4_mgmt, project_mem_dir, cwd, config)
    lines.extend(_aidocs_opening_lines(aidocs_entries, project_root))
    _project_hook_lines(lines, project_root, cwd, session_id)
    return state, lines, vector_flag


def _nudge_project_memory_layer(lines: List[str], state: Dict[str, Any], cwd: str, project_root: Optional[Path],
                                project_mem_dir: Optional[Path]) -> None:
    """專案還沒有記憶層 → 一行指向 init-project-memory.py；一個 session 只判一次（state project_memory_nudged）。"""
    try:
        if not state.get("project_memory_nudged"):
            nudge = _project_memory_nudge(cwd, project_root, project_mem_dir)
            state["project_memory_nudged"] = True
            lines.extend(nudge)
    except Exception as e:
        _atom_debug_error("project_memory_nudge", e)


def _user_identity(cwd: str) -> Tuple[str, List[str], bool]:
    """使用者、職能、是不是管理職。查到一半出錯就留已查到的，其餘用預設值（""、[]、False）。"""
    v4_user = ""
    v4_roles: List[str] = []
    v4_mgmt = False
    try:
        v4_user = get_current_user()
        bootstrap_personal_dir(cwd, v4_user)
        role_info = load_user_role(cwd, v4_user)
        v4_roles = list(role_info.get("roles") or [])  # 查不到職能＝[]，不預設 programmer
        v4_mgmt = is_management(cwd, v4_user)
    except Exception as e:
        _atom_debug_error("role_bootstrap", e)
    return v4_user, v4_roles, v4_mgmt


def _candidate_pool(state: Dict[str, Any], cwd: str, root_res: Any, root_rebuilt: bool,
                    project_mem_dir: Optional[Path], project_root: Optional[Path], v4_user: str, v4_roles: List[str],
                    v4_mgmt: bool, global_atoms: List[Any]) -> Tuple[Optional[Path], Dict[str, Any], List[Any], List[Any]]:
    """建候選池並記進 state：身分、atom_index（含專案根指紋）、本輪已注入清空、phase、aidocs；
    有 scope 分層的專案照職能重生 MEMORY.md。回 (組織層根, 候選池, 收窄後的全域 atom, aidocs 條目)。"""
    state["user_identity"] = {
        "user": v4_user,
        "roles": v4_roles,
        "management": v4_mgmt,
    }

    # 候選池單源（wg_atoms.build_candidate_pool，memory_search 同用）：local-realm 閘門、
    # scope 可見性（SPEC §8.1：personal 只給本人、role 只給持有者）、Supersedes 名單都在裡面收窄一次；
    # UPS 六條檢索路全從此池取，不再各自過濾。副作用（註冊、bootstrap、MEMORY.md 重生）留在本檔。
    org_root = org_memory_root()
    pool = build_candidate_pool(
        cwd, v4_user, v4_roles,
        org_root=str(org_root) if org_root else None, global_atoms=global_atoms,
    )
    global_atoms = pool["global"]
    project_atoms_merged = pool["project"]

    v4_layout_active = bool(project_mem_dir) and any(
        (project_mem_dir / d).is_dir() for d in SCOPE_DIR_NAMES
    )

    state["atom_index"] = {
        "global": pool["global"],
        "project": pool["project"],
        "org": pool["org"],
        "org_base": pool["org_base"],
        "project_memory_dir": pool["project_memory_dir"],
        "project_root": pool["project_root"],
        "project_root_fingerprint": root_res.fingerprint if root_res else "",
        "project_slug": pool["project_slug"],
        "scopes": pool["scopes"],
        "superseded": pool["superseded"],
    }
    state["injected_atoms"] = []
    if not root_rebuilt:
        state["phase"] = "working"

    if v4_layout_active and v4_user:
        _regenerate_role_filtered_memory_index(
            project_mem_dir, v4_user, v4_roles, v4_mgmt, project_atoms_merged,
        )

    aidocs_entries = parse_aidocs_index(project_root) if project_root else []
    aidocs_keywords = extract_aidocs_keywords(aidocs_entries) if aidocs_entries else {}
    state["aidocs"] = {
        "project_root": str(project_root) if project_root else "",
        "entries": [(f, d) for f, d, _kw in aidocs_entries],
        "keywords": aidocs_keywords,
    }
    return org_root, pool, global_atoms, aidocs_entries


def _global_index_zero_check(lines: List[str], g_names: List[str]) -> None:
    """全域 index 解析 fail-loud：全域 index 檔存在、磁碟上也有卡，卻解析出 0 atom＝解析失敗（_ATOM_INDEX.md 表內空行／檔截斷等）。
    不再 silent——log＋顯著提示。磁碟上一張卡都還沒有（新安裝）是合法的空層，不報。專案層可合法為空，故只查全域。"""
    try:
        if not g_names and (
            (MEMORY_DIR / "_atom_index.json").exists()
            or (MEMORY_DIR / "_ATOM_INDEX.md").exists()
            or (MEMORY_DIR / MEMORY_INDEX).exists()
        ) and has_indexable_cards(MEMORY_DIR):
            _atom_debug_error(
                "session_start:global_index_zero",
                RuntimeError(i18n.t("hook.session_start.index_zero_error")),
            )
            lines.append(i18n.t("hook.session_start.index_zero"))
    except Exception as e:
        _atom_debug_error("session_start:global_index_zero", e)


def _index_disk_check(lines: List[str], global_atoms: List[Any]) -> None:
    """索引載入後校驗：防 _atom_index.json 被 funnel 外改壞 → 注入鏈靜默降效。廉價雙向：index→disk 存在性＋
    memory/ 頂層→index 漏登。失配＝log（always-on）＋可見提示，不自動重建（避免與 funnel 互搶）。"""
    try:
        _idx_missing = [
            n for n, p, _t in global_atoms
            if p and not (CLAUDE_DIR / p).exists()
        ]
        _idx_paths = {
            str((CLAUDE_DIR / p).resolve()).lower()
            for _n, p, _t in global_atoms if p
        }
        # 磁碟側：memory/ 根層 *.md ＋ 各範疇資料夾（memory/<範疇>/**、含 Failures）
        # 遞迴；`_` 前綴目錄（_reference/_INDEX 等）與 skip 名單由 iter_realm_category_dirs 剪掉。
        _disk_candidates = list(MEMORY_DIR.glob("*.md"))
        if iter_realm_category_dirs is not None:
            for _cat_dir in iter_realm_category_dirs(MEMORY_DIR):
                _disk_candidates += [
                    f for f in _cat_dir.rglob("*.md")
                    if not any(part.startswith("_") for part in f.relative_to(_cat_dir).parts)
                ]
        _disk_orphans = [
            f.stem for f in _disk_candidates
            if not f.name.startswith("_") and f.name != MEMORY_INDEX
            and str(f.resolve()).lower() not in _idx_paths
        ]
        if _idx_missing or _disk_orphans:
            _atom_debug_error(
                "session_start:index_validate",
                RuntimeError(
                    i18n.t("hook.session_start.index_mismatch_error",
                           missing=_idx_missing[:5], unindexed=_disk_orphans[:5])
                ),
            )
            lines.append(i18n.t("hook.session_start.index_validate", missing=len(_idx_missing), orphans=len(_disk_orphans)))
    except Exception as e:
        _atom_debug_error("session_start:index_validate", e)


def _skill_index_check(lines: List[str], config: Dict[str, Any]) -> None:
    """skill 計數 SoT 防呆：補 PostToolUse 自動同步漏接者（如 Bash 刪 skill 目錄）。實檔 skills/*/SKILL.md 數 ≠
    _skill_index.json count → 提示跑 tools/skill-index.py --write。不自動改檔（與 PostToolUse 自動同步分工）。"""
    try:
        if not (config or {}).get("skill_index", {}).get("enabled", True):
            return
        import json as _json
        _sk_dir = CODE_DIR / "skills"   # skills 屬程式碼樹（與 tools／hooks／lib 並列）
        _true_sk = sum(1 for _ in _sk_dir.glob("*/SKILL.md"))
        _idx = _sk_dir / "_skill_index.json"
        _idx_n = None
        if _idx.exists():
            try:
                _idx_n = _json.loads(
                    _idx.read_text(encoding="utf-8-sig")).get("count")
            except (ValueError, OSError):
                _idx_n = None
        if _idx_n != _true_sk:
            lines.append(i18n.t("hook.session_start.skill_index_mismatch", actual=_true_sk, indexed=_idx_n))
    except Exception as e:
        _atom_debug_error("session_start:skill_index_validate", e)


def _vector_flag_lines(lines: List[str], config: Dict[str, Any]) -> Optional[str]:
    """向量服務旗標：ping 一次更新旗標。服務還在才問索引狀態（服務沒回應就不問：Windows 連關著的埠要等滿逾時，
    多問一次就多等一秒）；服務沒回應看要不要出紅字。回旗標結果（出錯回 "error"），收尾段看它決定要不要再 ping。"""
    try:
        vector_flag = _refresh_vector_flag(config)
    except Exception as e:
        vector_flag = "error"
        _atom_debug_error("SessionStart:vector_flag_refresh", e)
    if vector_flag == "kept":
        lines.extend(_vector_index_advisory(config))
    elif vector_flag == "cleared":
        _notify_vector_down(config)
    return vector_flag


def _role_lines(lines: List[str], v4_user: str, v4_roles: List[str], v4_mgmt: bool,
                project_mem_dir: Optional[Path], cwd: str, config: Dict[str, Any]) -> None:
    """查得到使用者才有：[Role] 一行、待審草稿數；userExtraction 開著時再加最近 24 小時自動萃取了幾張卡。"""
    if not v4_user:
        return
    lines.append(
        f"[Role] user={v4_user} roles={','.join(v4_roles) or '-'} mgmt={v4_mgmt}"
    )
    # 待審草稿對所有人顯示（裁決資格另由 config review.deciders 決定）
    pending = _count_pending_review(project_mem_dir)
    if pending > 0:
        lines.append(i18n.t("hook.session_start.pending_review", n=pending))

    if config.get("userExtraction", {}).get("enabled", False):
        try:
            v41_count = _count_recent_auto_atoms(v4_user, cwd, hours=24)
            if v41_count > 0:
                lines.append(i18n.t("hook.session_start.auto_atoms_yesterday", n=v41_count))
        except Exception as e:
            _atom_debug_error("daily_push", e)


def _project_hook_lines(lines: List[str], project_root: Optional[Path], cwd: str, session_id: str) -> None:
    """專案自己掛的 session_start hook（_shared._call_project_hook，子行程隔離）回的提示行，空行略過。"""
    if not project_root:
        return
    try:
        ph_result = _call_project_hook(
            project_root, "session_start",
            {"cwd": cwd, "session_id": session_id},
        )
        if not ph_result:
            return
        for extra_line in ph_result.get("lines", []):
            if extra_line:
                lines.append(extra_line)
    except Exception as e:
        _atom_debug_error("project_hook:session_start", e)


# ─── 兩條路匯合後 ────────────────────────────────────────────────────────────


def _reinject_compact_cards(lines: List[str], state: Dict[str, Any], compact_blob: str, compact_sent: List[str],
                            compact_unresolved: List[str]) -> None:
    """規則 COMPACT_REINJECT_AT_SESSION_START：壓縮前已注入的卡內文送回來；送出的卡算回已注入（使用偵測、效用歸因接得上），
    記號讓 PostCompact 不暫存、PostToolBatch 不重送。新開場那條中途降級退出就到不了這裡、沒有記號，照舊由 PostToolBatch 補位。"""
    if not compact_blob:
        return
    lines.append(compact_blob)
    state["compact_reinjected_at"] = _now_iso()
    state["injected_atoms"] = list(dict.fromkeys((state.get("injected_atoms") or []) + compact_sent))
    if compact_unresolved:   # 索引查不到的卡留痕，不靜默少送
        state["pending_reinjection_unresolved"] = compact_unresolved


def _marker_file_notices(lines: List[str], cwd: str) -> None:
    """標記檔提示（標記檔讀後清）：本地範疇指標、上個 session 自動歸類搬移的卡、SessionEnd 沒跑完的殘留、效用歸因 unknown 偏高。"""
    # V5+ realm：本地範疇 catalog 一行指標（僅核心環境 cwd∈~/.claude）
    lines.extend(_local_catalog_lines(cwd))
    _realm_automove_notice(lines)

    # SessionEnd 哨兵殘留檢查：殘留＝上個 session 的收尾流程未跑完即中斷
    # （harness timeout / 例外）→ 浮一行告警後清（收尾擁擠不得靜默失敗）。
    try:
        _check_se_sentinel_residual(lines)
    except Exception as e:
        print(f"SE-sentinel check error: {e}", file=sys.stderr)

    _outcome_unknown_notice(lines)


def _realm_automove_notice(lines: List[str]) -> None:
    """V5+ Realm 維度：上個 session 自動歸類搬移的卡不靜默提示（列卡名，附要同步的說明文件），讀後清標記檔；出錯寫 stderr。"""
    try:
        if not REALM_AUTOMOVE_MARKER.exists():
            return
        try:
            _rm = json.loads(REALM_AUTOMOVE_MARKER.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _rm = []
        if isinstance(_rm, list) and _rm:
            _names = ", ".join(
                (i18n.t("hook.session_start.slug_via", slug=m.get('slug','?'), via=m.get('via'))
                 if m.get("via") in ("LLM", "Else") else m.get("slug", "?"))
                for m in _rm[:6]
            )
            _more = i18n.t("hook.session_start.realm_more_many", n=len(_rm)) if len(_rm) > 6 else i18n.t("hook.session_start.realm_more_few", n=len(_rm))
            lines.append(i18n.t("hook.session_start.realm_moved_local", names=_names, more=_more))
            # 移檔後 doc-sync（user 補充）：舊 path/檔名引用提示需同步的說明文件
            _drefs = _realm_doc_refs(_rm)
            if _drefs:
                _parts = i18n.t("hook.session_start.part_sep").join(
                    f"{_k}→{', '.join(sorted(set(_v)))}" for _k, _v in list(_drefs.items())[:4]
                )
                lines.append(i18n.t("hook.session_start.realm_doc_refs", parts=_parts))
        try:
            REALM_AUTOMOVE_MARKER.unlink()
        except OSError:
            pass
    except Exception as e:
        print(f"[realm] automove notice error: {e}", file=sys.stderr)


def _realm_doc_refs(moved: List[Any]) -> Dict[str, List[str]]:
    """搬移紀錄各筆的 doc_refs（卡名 → 還引用舊路徑、要同步的說明文件）併成一份。"""
    _drefs: Dict[str, List[str]] = {}
    for _m in moved:
        for _k, _v in (_m.get("doc_refs") or {}).items():
            _drefs.setdefault(_k, []).extend(_v or [])
    return _drefs


def _outcome_unknown_notice(lines: List[str]) -> None:
    """效用歸因遙測：上個 session 判定 unknown 的比率連續偏高 → 送標記檔裡那一行，讀後清標記檔；出錯寫 stderr。"""
    try:
        _ow_marker = WORKFLOW_DIR / "outcome-unknown-advisory.json"
        if not _ow_marker.exists():
            return
        try:
            _ow = json.loads(_ow_marker.read_text(encoding="utf-8"))
            if _ow.get("msg"):
                lines.append(_ow["msg"])
        except (OSError, json.JSONDecodeError):
            pass
        try:
            _ow_marker.unlink()
        except OSError:
            pass
    except Exception as e:
        print(f"Outcome-watch notice error: {e}", file=sys.stderr)


def _review_notices(lines: List[str], input_data: Dict[str, Any], config: Dict[str, Any], session_id: str,
                    source: str, state: Dict[str, Any]) -> None:
    """回顧提示：定期回顧到期（記 state review_due）、atom 震盪警告、插件點 SessionStart。"""
    try:
        review_reminder = _check_periodic_review_due(config)
        if review_reminder:
            lines.append(review_reminder)
            state["review_due"] = True
    except Exception as e:
        print(f"Review check error: {e}", file=sys.stderr)

    try:
        osc_warning = _load_oscillation_warnings(state, config)
        if osc_warning:
            lines.append(osc_warning)
    except Exception as e:
        print(f"Oscillation load error: {e}", file=sys.stderr)

    lines.extend(wg_plugins.emit("SessionStart", state=state, config=config, session_id=session_id,
                                 input_data=input_data, source=source))


def _service_notices(lines: List[str]) -> None:
    """服務檢查：遠端萃取後端長期掛掉（Long DIE）、MCP server 狀態。"""
    try:
        long_die = check_long_die_status()
        if long_die:
            backend_name = long_die.get("backend", "remote")
            until = long_die.get("until", "?")
            lines.append(i18n.t("hook.session_start.long_die", backend=backend_name, until=until))
    except Exception as e:
        print(f"[dual-backend] Long DIE check error: {e}", file=sys.stderr)

    try:
        mcp_issues = _check_mcp_servers()
        if mcp_issues:
            lines.append("[MCP] " + "; ".join(mcp_issues))
    except Exception as e:
        print(f"[mcp-health] Check error: {e}", file=sys.stderr)


def _contract_sentinels(lines: List[str], v6: bool) -> None:
    """可觀測性：IDENTITY.md 完整性哨兵（寫碼守門開才查）與必載檔硬契約哨兵（登記表驅動；USER.md 已由 user-init.sh
    從 USER-{user}.md 拷好）。兩者都看資料根；資料根沒有這些檔就不查（沒用個人化啟動不算降級）。"""
    try:
        if v6:
            lines.extend(check_identity_integrity(CLAUDE_DIR))
    except Exception as e:
        print(f"identity integrity check error: {e}", file=sys.stderr)
    try:
        lines.extend(check_always_load_contracts(CLAUDE_DIR))
    except Exception as e:
        print(f"always-load contract check error: {e}", file=sys.stderr)


# ─── 寫 state 之後 ───────────────────────────────────────────────────────────


def _cleanup_and_refresh_vector_flag(config: Dict[str, Any], vector_flag: Optional[str], lines: List[str]) -> None:
    """清舊 state；vector_flag 是 None（沿用舊 state 那條沒有健檢段、沒 ping 過）就在這裡 ping 向量服務。
    服務已暖則保留 flag（省冷啟動 no_flag 空窗），只有 health ping 失敗才拆（fail-closed）。
    這一 ping 也照新開場那樣看服務狀態、補向量的開場紅字：服務狀態在兩場之間會變，resume／compact 一樣要報。"""
    try:
        _cleanup_old_states()
    except Exception as e:
        print(f"SessionStart cleanup error: {e}", file=sys.stderr)

    if vector_flag is None:
        _vector_flag_lines(lines, config)
        lines.extend(degraded_session_lines())


def _spawn_vector_starter(config: Dict[str, Any], state: Dict[str, Any], session_id: str) -> None:
    """Vector service：fire-and-forget 啟動器（自癒／觀測邏輯在 starter.py）。設定關掉、同 cwd 已有活著的 session、
    或這次已降級就不起：輸出送出後行程還得在上限內結束（Claude Code 取消時不保證採用已印的輸出）。"""
    _vector_cfg = config.get("vector_search", {})
    if not (_vector_cfg.get("enabled", True) and _vector_cfg.get("auto_start_service", True)
            and not state.get("_skip_vector_init") and not _hook_timing.degraded()):
        return
    import vector_venv   # lib/
    if vector_venv.plugin_edition() and not vector_venv.lancedb_available():
        return   # 插件版沒跑 setup-vector：連 starter 都不起（開場邀請那行已講）
    try:
        starter = CODE_DIR / "tools" / "memory-vector-service" / "starter.py"
        _bg_kwargs: dict = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        _bg_kwargs.update(background_spawn_kwargs())
        subprocess.Popen(
            [sys.executable, str(starter),
             "--phase", "sessionstart", "--session-id", session_id or ""],
            **_bg_kwargs,
        )
    except Exception as e:
        _atom_debug_error("注入:vector_service_bg", e)
