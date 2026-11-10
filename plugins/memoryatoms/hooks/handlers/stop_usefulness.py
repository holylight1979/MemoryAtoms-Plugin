"""stop_usefulness.py — 卡片效用結算：回收本回合讀過的檔、稽核「指標卡有沒有被取用」、判這回合結果（成功／失敗）、把注入的卡歸因成有用或無用。
由 handlers/stop.py 的 handle_stop 叫用；本檔不 import 入口檔。
"""

import json
import sys
import time
from typing import Any, Dict, List, Optional
from pathlib import Path
from wg_core import _atom_debug_error, _now_iso
import i18n   # lib/：給人看的提示走語言表
from wg_release_rules import user_turn as _user_turn, in_turn
from wg_evasion import claims_completion, get_current_turn_text


_ACCESSED_FILES_CAP = 500  # accessed_files state 條目上限（超出裁最舊）


def _own_failing_tests(state: Dict[str, Any], session_id: str) -> List[Dict[str, Any]]:
    """failing_tests 裡 session_id == 本 session 的條目（沒蓋 sid 的舊條目不算自己的）。"""
    return [f for f in (state.get("failing_tests") or []) if isinstance(f, dict) and f.get("session_id") == session_id]


def _harvest_accessed_files(state: Dict[str, Any], transcript_text: str) -> bool:
    """從共用 transcript 尾段一次回收 Read 過的檔案到 accessed_files；回是否有新增。

    取代 PostToolUse matcher 含 Read 的做法（每次讀檔 spawn 一個 hook 行程，
    只為記一筆路徑）。同路徑只記首見。已知邊界（消費者 handoff 提示 /
    codex 脈絡皆 best-effort 訊號，可接受）：
      - 尾窗（read_transcript_tail）涵蓋不到的極早期讀取會漏
      - `at` 為回收時刻而非讀檔時刻；同 turn 內其他 Stop hook 讀到的
        accessed_files 慢一個 turn 邊界
    """
    if not transcript_text:
        return False
    accessed = state.setdefault("accessed_files", [])
    seen = {a.get("path") for a in accessed if isinstance(a, dict)}
    added = False
    trimmed = False
    for raw in transcript_text.splitlines():
        # 廉價預篩：多數行連 tool_use/Read 字樣都沒有，免逐行 json.loads
        if '"tool_use"' not in raw or '"Read"' not in raw:
            continue
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            continue
        content = obj.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") == "Read"
            ):
                fp = (block.get("input") or {}).get("file_path", "")
                if fp and fp not in seen:
                    seen.add(fp)
                    accessed.append({"path": fp, "at": _now_iso()})
                    added = True
    # 上限 500（重讀量大的長 session 不無限累積 state）：裁最舊；被裁路徑之後
    # 再被 Read 會重新回收（消費端皆 best-effort 統計，可接受）。
    if len(accessed) > _ACCESSED_FILES_CAP:
        state["accessed_files"] = accessed[-_ACCESSED_FILES_CAP:]
        trimmed = True
    return added or trimmed


# ─── 取用端閉環稽核（AtomAudit Gate） ───────────────────────────


def _normalize_read_path(p: str) -> str:
    return (p or "").replace("\\", "/").lower()


_ATOM_AUDIT_LIST_MAX = 3  # 收尾訊息最多列名幾顆（其餘以計數帶過）


def _audit_pointer_atom_consumption(
    state: Dict[str, Any],
) -> Optional[tuple]:
    """取用端閉環：trigger 命中（＝與本場任務域吻合）但僅以一行路標注入
    （budget skip / cold）、且整場未 Read 的 atom → 回 (reason, names) 供
    Stop 閘要求三選一表態；無候選回 None。

    寫入端有閘（write-gate/funnel），取用端此前全靠模型自律——路標的具體性
    輸給 context 實體線索時就漏（實證：trigger 命中的肥 atom 被 budget skip
    成一行、全程未展開）。防噪內建：
      - 只稽核 source=trigger（bm25/vector/related 不催——related 擴散本就低確信）
      - 本回合（使用者打字開的回合起，同伴訊息、背景通知不另開回合）注入的不催（至少給模型一個完整回合決定要不要展開）
      - 消費判定用 Stop 已回收的 accessed_files（同 turn 的 Read 先於本判定入 state）
    """
    inj_log = state.get("injection_log") or []
    if not inj_log:
        return None
    turn_seq = _user_turn(state, int(state.get("turn_seq", 0)))
    prompted = set(state.get("atom_audit_prompted") or [])
    accessed = {
        _normalize_read_path(a.get("path"))
        for a in (state.get("accessed_files") or [])
        if isinstance(a, dict)
    }
    seen: set = set()
    candidates: List[Dict[str, Any]] = []
    for rec in inj_log:
        if not isinstance(rec, dict):
            continue
        name = rec.get("name") or ""
        if not name or name in prompted or name in seen:
            continue
        if rec.get("source") != "trigger" or rec.get("form") not in ("skip", "cold"):
            continue
        if turn_seq and int(rec.get("turn_seq", 0)) >= turn_seq:
            continue  # 本 turn 才注入——不催
        rec_path = _normalize_read_path(rec.get("path"))
        suffix = f"/{name.lower()}.md"
        if any(ap == rec_path or ap.endswith(suffix) for ap in accessed):
            continue  # 已 Read（consumed）
        seen.add(name)
        candidates.append(rec)
    if not candidates:
        return None

    msg: List[str] = [
        i18n.t("hook.stop_usefulness.audit_head"),
    ]
    for rec in candidates[:_ATOM_AUDIT_LIST_MAX]:
        # 一律用絕對 path（rel 只相對該 atom 自己的 realm root，跨 realm 會斷鏈）
        msg.append(f"  - {rec['name']} → Read {rec.get('path') or rec.get('rel', '')}")
    if len(candidates) > _ATOM_AUDIT_LIST_MAX:
        msg.append(i18n.t("hook.stop_usefulness.audit_more", count=len(candidates) - _ATOM_AUDIT_LIST_MAX))
    msg.append(i18n.t("hook.stop_usefulness.audit_ask"))
    return "\n".join(msg), [r["name"] for r in candidates]


def _budgeted_embed_fn(raw_embed_fn, uconf: Dict[str, Any]):
    """embedding tiebreak 的全 turn 共用時間預算包裝。

    單次 tiebreak 最壞 2×embed_timeout_s（兩段 embed），多顆 atom 疊加會頂到
    Stop hook 上限。累計耗時超過 usefulness.embed_budget_s（預設 3s）→ 之後
    一律回 None（detect_atom_use 退回 lexical 主判）。預算耗盡浮 stderr 一行
    （fail-open 必告知）。raw_embed_fn 為 None 時原樣回 None。
    """
    if raw_embed_fn is None:
        return None
    try:
        budget_s = float(uconf.get("embed_budget_s", 3.0))
    except (TypeError, ValueError):
        budget_s = 3.0
    if budget_s <= 0:
        return raw_embed_fn
    spent = [0.0]
    exhausted_warned = [False]

    def _fn(a: str, b: str):
        if spent[0] >= budget_s:
            if not exhausted_warned[0]:
                exhausted_warned[0] = True
                print(
                    f"[usefulness] embed tiebreak turn budget exhausted "
                    f"({spent[0]:.1f}s ≥ {budget_s}s) — falling back to lexical",
                    file=sys.stderr,
                )
            return None
        t0 = time.monotonic()
        try:
            return raw_embed_fn(a, b)
        finally:
            spent[0] += time.monotonic() - t0

    return _fn


# ─── 注入→使用→結果 閉環歸因 ───────────────────────────────────


def _detect_turn_outcome(
    state: Dict[str, Any], last_text: str, session_id: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Optional[bool]:
    """3 值 success 偵測（複用既有訊號）。回 True(+1)/False(0)/None(unknown=no-op)。

      - 0（fail）：**本 turn** failing_tests 非空 / 本 turn evasion_flag /
        wisdom_retry_count≥2（error / 糾正 / retry / evasion 任一）；
        本回合使用者糾正（usefulness.friction_as_fail，預設開）→ fail。
      - +1（success）：宣告完成（claims_completion）且無上述 fail 訊號（硬正向）。
      - None（unknown）：既無完成宣告也無 fail 訊號 → 不動 (α,β)，防雜訊污染。

    failing_tests 只認本回合（規則 TURN_SINCE_USER_PROMPT：使用者打字開的回合起，同伴訊息、背景通知不另開回合）：
    全量累積會讓早前回合的舊失敗污染後續每個回合的 outcome。無 turn_seq 的 entry（升級前
    in-flight session）保守視為本回合（fail-open，不漏 fail 訊號）。sync gate /
    TestFailGate 等其他消費者維持全量語意不變。
    """
    turn_seq = int(state.get("turn_seq", 0))
    since = _user_turn(state, turn_seq)
    pool = _own_failing_tests(state, session_id) if session_id else (state.get("failing_tests") or [])
    failing = [
        f for f in pool
        if in_turn((f or {}).get("turn_seq", turn_seq), turn_seq, since)
    ]
    evasion = bool(state.get("evasion_flag"))
    # wisdom_retry_count 是 session 累計（FixEscalation 等消費者要它累計）；
    # outcome 只看本 turn 的增量——UPS 在 turn 起點快照 base。沒有 base（升級前
    # in-flight session）→ 沿用累計值，不漏 fail 訊號。
    retry_total = int(state.get("wisdom_retry_count", 0) or 0)
    base = state.get("wisdom_retry_turn_base")
    retry = retry_total - int(base) if base is not None else retry_total
    corrected = False
    if ((config or {}).get("usefulness", {}) or {}).get("friction_as_fail", True):
        from wg_friction import correction_in_turn
        corrected = correction_in_turn(state, turn_seq, since, config)
    if failing or evasion or retry >= 2 or corrected:
        return False
    if last_text and claims_completion(last_text):
        return True
    return None


def _bump_outcome_stats(state: Dict[str, Any], outcome: Optional[bool]) -> None:
    """per-turn outcome 三值計數（success/fail/unknown）。SessionEnd flush 成
    unknown 比率遙測——unknown 系統性偏高＝完成語 regex 失配、α/β 晉升軌
    靜默停滯的早期訊號。與 α/β 歸因同守 once-per-turn（caller 已以
    usefulness_attributed_seq 守門）。"""
    stats = state.setdefault(
        "outcome_stats", {"success": 0, "fail": 0, "unknown": 0}
    )
    key = "success" if outcome is True else "fail" if outcome is False else "unknown"
    stats[key] = int(stats.get(key, 0)) + 1


def _sub_waiting(rec: Dict[str, Any]) -> bool:
    """背景子代理派出後還沒收尾（SubagentStop 還沒回填，status 仍是 async_launched）。"""
    return str(rec.get("status", "") or "").lower() == "async_launched"


def _attribute_usefulness(
    state: Dict[str, Any], config: Dict[str, Any], session_id: str,
    transcript, last_text: str, transcript_text: Optional[str] = None,
) -> None:
    """per-turn 注入→使用→結果歸因：對 turn_injected + 本 turn sub-agent 注入中
    「被判 used 且 outcome 決定性」者 → record_usefulness(α/β，走 funnel)。

    once-per-turn：以 turn_seq 守門（blocked turn 多次 Stop 不重複計）；同一 turn 後來才收到結果的子代理紀錄
    （規則 SUBAGENT_RESULT_FROM_SUBAGENT_STOP）照結算，主回合注入與 outcome 計數不重算。fail-open。
    transcript_text 給定時（caller 以 read_transcript_tail 共用尾段）turn 文字
    從該字串解析、不再開檔；None 則自行讀（相容直接呼叫的測試）。
    """
    try:
        uconf = (config or {}).get("usefulness", {}) or {}
        if not uconf.get("enabled", True):
            return
        turn_seq = int(state.get("turn_seq", 0))
        turn_done = bool(turn_seq) and state.get("usefulness_attributed_seq") == turn_seq
        if turn_done and not any(isinstance(r, dict) and not r.get("attributed") and not _sub_waiting(r)
                                 for r in state.get("subagent_injections") or []):
            return  # 本 turn 已歸因，也沒有新收到結果的子代理紀錄

        from lib.atom_access import record_usefulness
        from wg_atoms import (
            detect_atom_use, detect_atom_use_v2, resolve_atom_path, make_embed_tiebreak_fn,
        )

        rare_min = int(uconf.get("rare_token_min", 2))
        overlap_min = float(uconf.get("lexical_overlap_min", 0.18))
        embed_fn = _budgeted_embed_fn(make_embed_tiebreak_fn(config), uconf)
        # 判用政策：v2＝行動證據（rescue 命中）優先、否定線索、路標未讀不算、去路徑噪音後
        # 共享 ≥6 才算（標註集 57 筆：precision 0.35→0.57、recall 0.84）；v1＝舊詞彙重疊（回滾用）。
        policy = str(uconf.get("attribution_policy", "v2"))
        v2_shared_min = int(uconf.get("v2_shared_min", 6))
        v2_containment_min = float(uconf.get("v2_containment_min", 0.0))

        turn_text = (
            get_current_turn_text(transcript, text=transcript_text)
            if (transcript or transcript_text) else ""
        )
        outcome = _detect_turn_outcome(state, last_text, session_id, config)
        if not turn_done:
            _bump_outcome_stats(state, outcome)

        turn_seq_now = int(state.get("turn_seq", 0))
        rescue_map: Dict[str, List[str]] = {}
        form_map: Dict[str, str] = {}
        if policy == "v2":
            try:
                from wg_rescue import rescue_hits_for_turn, rescue_hits_for_turn_from_state
                rescue_map = rescue_hits_for_turn_from_state(state, turn_seq_now)
                if not rescue_map and "rescue_hits_by_turn" not in state:
                    rescue_map = rescue_hits_for_turn(session_id, turn_seq_now)  # 升級前 in-flight：退回讀 log
            except Exception as e:
                _atom_debug_error("usefulness:rescue_map", e)
            for rec in (state.get("injection_log") or []):
                if int(rec.get("turn_seq", -1) or -1) == turn_seq_now:
                    form_map[rec.get("name", "")] = rec.get("form_final") or rec.get("form", "ok")

        def _read(path_str: str) -> str:
            try:
                return Path(path_str).read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError, ValueError):
                return ""

        # 同一顆 atom 同一 turn 只寫一筆：先收齊各來源（主回合 / 子代理）的判定，
        # 結果一致才寫；父成功子失敗這種衝突 → unknown、不動 α/β、只留紀錄。
        pending: Dict[str, Dict[str, Any]] = {}

        def _consider(name: str, path_str: str, content: str, match_text: str,
                      decided: Optional[bool], *, origin: str = "parent") -> None:
            if decided is None or not content or not match_text:
                return
            if policy == "v2":
                # 子代理只用自己的產出判用：不借父回合文字、不借父回合 rescue（那是父的工具）；
                # 子代理拿到的是緊湊全文 blob，form 視為 ok。
                det = detect_atom_use_v2(
                    content, match_text, atom_name=name,
                    form=(form_map.get(name, "ok") if origin == "parent" else "ok"),
                    rescue_tokens=(rescue_map.get(name) if origin == "parent" else None),
                    shared_min=v2_shared_min, containment_min=v2_containment_min,
                )
            else:
                det = detect_atom_use(
                    content, match_text,
                    rare_token_min=rare_min, overlap_min=overlap_min, embed_fn=embed_fn,
                )
            if not det.get("used"):
                return
            slot = pending.setdefault(name, {"path": path_str, "outcomes": set()})
            slot["outcomes"].add(bool(decided))

        # 1) UPS per-turn 注入（turn_injected）— 比對本 turn assistant 活動文字；本 turn 已結算過就不重算
        for entry in ([] if turn_done else state.get("turn_injected") or []):
            name = entry.get("name", "")
            path_str = entry.get("path", "")
            if not name or not path_str:
                continue
            _consider(name, path_str, _read(path_str), turn_text, outcome)

        # 2) sub-agent 注入（state["subagent_injections"]），規則 SUBAGENT_RESULT_FROM_SUBAGENT_STOP：收到結果才結。
        #    背景子代理還沒收尾（status 仍是 async_launched）→ 標 waiting、不結，不管是哪個回合派出的；
        #    收到結果的看收到的回合（result_turn_seq，沒有才看派出的 turn_seq）落在本回合才結算（規則 TURN_SINCE_USER_PROMPT：
        #    使用者打字開的回合起，同伴訊息、背景通知不另開回合），更早回合收到卻沒結算的標過期不套當輪 outcome。
        #    use 偵測比對該 agent 的 output_summary（其實際產物）；outcome 疊 agent 狀態。
        since = _user_turn(state, turn_seq)
        for rec in (state.get("subagent_injections") or []):
            if not isinstance(rec, dict) or rec.get("attributed"):
                continue
            if _sub_waiting(rec):
                rec["waiting"] = True
                continue
            rec_turn = rec.get("result_turn_seq", rec.get("turn_seq"))
            if rec_turn is not None and turn_seq and not in_turn(rec_turn, turn_seq, since):
                if int(rec_turn) < since:
                    rec["attributed"] = True
                    rec["skipped"] = "stale_turn"
                continue
            status = str(rec.get("status", "") or "").lower()
            sub_outcome = outcome
            if any(k in status for k in ("error", "fail", "abort", "cancel")):
                sub_outcome = False  # agent 出錯 → fail（覆寫 turn outcome）
            # 只看子代理自己的產出；接上父回合全文會讓「父採用、子沒採用」變成假的結果衝突（Codex #8 反例）。
            # v1 政策維持舊行為（父文字併入）供回滾。
            sub_text = rec.get("output_summary", "") or ""
            match_text = sub_text if policy == "v2" else (sub_text + "\n" + turn_text)
            if not match_text.strip():
                rec["attributed"] = True
                rec["skipped"] = "no_output"
                continue
            for aname in (rec.get("atoms") or []):
                p = resolve_atom_path(aname)
                if p is None:
                    continue
                _consider(aname, str(p), _read(str(p)), match_text, sub_outcome, origin="subagent")
            rec["attributed"] = True  # 本 turn 已處理（含 no-op，避免下 turn 重算）

        attributed = []  # (atom, success) for telemetry
        conflicted: List[str] = []
        for name, slot in pending.items():
            if len(slot["outcomes"]) != 1:
                conflicted.append(name)
                continue
            decided = next(iter(slot["outcomes"]))
            record_usefulness(Path(slot["path"]), used=True, success=decided, source="hook:usefulness")
            attributed.append((name, decided))

        if turn_seq:
            state["usefulness_attributed_seq"] = turn_seq
        if attributed or conflicted:
            entry = {
                "turn_seq": turn_seq,
                "outcome": ("+1" if outcome is True else "0" if outcome is False else "unknown"),
                "atoms": [{"atom": a, "success": s} for a, s in attributed],
                "at": _now_iso(),
            }
            if conflicted:
                entry["conflicted"] = conflicted
            state.setdefault("usefulness_log", []).append(entry)
            state["usefulness_log"] = state["usefulness_log"][-50:]
    except Exception as e:
        print(f"usefulness attribution error: {e}", file=sys.stderr)
