"""
wg_atoms_budget.py — 預算裁切：決定每張卡整段注入或只給指標、熱冷分類、冷卡單行、在 token 預算內載入 atom、組出最終注入文字。

誰叫它：每句提問的注入管線（ups_inject 等）、subagent 注入。
從 wg_atoms.py 拆出；hooks/wg_atoms.py 仍 re-export 這裡的名稱。
"""

import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from wg_core import MEMORY_DIR, TURN_BUDGET_LIMIT, _estimate_tokens
import i18n   # lib/：給人看的提示走語言表
from pathlib import Path
from wg_atoms_index import AtomEntry, load_access_cached, parse_memory_index
from wg_atoms_match import BM25_MIN_SCORE_DEFAULT, bm25_match, compute_activation, match_triggers
from wg_atoms_pool import superseded_names_cached
from wg_atoms_render import (
    _KNOWLEDGE_CAP_TOKENS_DEFAULT,
    _extract_named_section,
    _knowledge_cap_for_budget,
    _strip_atom_for_injection,
    _strip_atom_for_injection_impression_only,
    _truncated_mark,
)


_TURN_BUDGET_LIMIT = TURN_BUDGET_LIMIT  # 舊名 re-export（caller/verify 鎖定此名）


def decide_atom_injection(
    raw_content: str,
    full_content: str,
    used_tokens: int,
    budget_limit: int = _TURN_BUDGET_LIMIT,
) -> Tuple[str, str, int]:
    """Decide ok / fallback / skip for an atom against per-turn budget.

    full_content 是預設 cap 截過的版本、預算還有剩時，改用依剩餘預算放寬 cap 的版本
    （候選卡少時整張送到）；放寬版塞不下就照舊用 full_content。full_content 若不是
    預設 strip 的產物（例如呼叫端只抽了命中章節）就不動它。"""
    if _truncated_mark() in full_content:
        remaining = budget_limit - used_tokens
        if (_knowledge_cap_for_budget(remaining) > _KNOWLEDGE_CAP_TOKENS_DEFAULT
                and full_content == _strip_atom_for_injection(raw_content)):
            wide = _strip_atom_for_injection(raw_content, remaining_budget=remaining)
            wide_tokens = _estimate_tokens(wide)
            if wide != full_content and used_tokens + wide_tokens <= budget_limit:
                return ("ok", wide, wide_tokens)
    full_tokens = _estimate_tokens(full_content)
    if used_tokens + full_tokens <= budget_limit:
        return ("ok", full_content, full_tokens)

    fb_content = _strip_atom_for_injection_impression_only(raw_content)
    fb_tokens = _estimate_tokens(fb_content)
    if fb_tokens >= full_tokens:
        return ("skip", "", 0)
    if used_tokens + fb_tokens <= budget_limit:
        return ("fallback", fb_content, fb_tokens)
    return ("skip", "", 0)


_HOT_RECENT_DAYS = 7


_HOT_RECENT_WINDOW_SEC = _HOT_RECENT_DAYS * 86400


_COLD_LINE_CAP = 80


_STATUS_LINE_RE = re.compile(r"^- Status:\s*(.+)$", re.MULTILINE)


_STATUS_CAP = 40


def atom_status_suffix(raw_content: str) -> str:
    """atom 選填 `- Status:` 現況行 → 一行注入（cold / budget skip）的附帶字串。

    肥 atom 被降為一行路標時，不展開也保有最低現況資訊量（如「案結」＝
    收尾期非爭議期）。無 Status 行回空字串。"""
    m = _STATUS_LINE_RE.search(raw_content or "")
    if not m:
        return ""
    val = m.group(1).replace("\n", " ").replace("\r", " ").strip()
    if not val:
        return ""
    if len(val) > _STATUS_CAP:
        val = val[:_STATUS_CAP].rstrip() + "…"
    return f" [Status: {val}]"


def _recent_count(timestamps: Any) -> int:
    """timestamps 清單中落在 7d 窗內的筆數（_recent_reads_7d / cache 路徑共用）。"""
    if not isinstance(timestamps, list):
        return 0
    now = time.time()
    count = 0
    for ts in timestamps:
        try:
            if now - float(ts) <= _HOT_RECENT_WINDOW_SEC:
                count += 1
        except (TypeError, ValueError):
            continue
    return count


def _recent_reads_7d(access_file: Path) -> int:
    if not access_file.exists():
        return 0
    try:
        data = json.loads(access_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    timestamps = data.get("timestamps", []) if isinstance(data, dict) else []
    return _recent_count(timestamps)


def classify_hot_cold(
    atom_path: Path, source: str, hot_recent_threshold: int = 3,
    access_cache: Optional[Dict[str, Dict[str, Any]]] = None,
) -> str:
    if source == "trigger":
        return "hot"
    if access_cache is not None:
        data = load_access_cached(atom_path, access_cache)
        recent = _recent_count(data.get("timestamps"))
    else:
        access_file = atom_path.parent / f"{atom_path.stem}.access.json"
        recent = _recent_reads_7d(access_file)
    return "hot" if recent >= hot_recent_threshold else "cold"


def pointer_path(
    atom_path: Optional[Path] = None, rel_path: str = "", name: str = "",
) -> str:
    """一行路標的路徑渲染：有實檔路徑就給絕對路徑（正斜線）。

    atom 分屬多個 realm root（~/.claude 與各專案 .claude），rel_path 只相對它自己
    那顆 root；消費端（模型）拿到裸相對路徑只能以 cwd 解析 → 跨 realm 必斷鏈，
    最壞是解析到同名的另一顆檔。故路標一律絕對化；atom_path 缺席（legacy caller）
    才退回 rel_path。
    """
    if atom_path is not None:
        return Path(atom_path).as_posix()
    return rel_path or f"{name}.md"


_NAME_ECHO_STRIP_RE = re.compile(r"[\W_]+")


def title_echoes_name(title: str, name: str) -> bool:
    """規則 POINTER_NO_NAME_ECHO：一行指標的標題／摘要只是卡名再寫一次（卡名就是標題轉成的檔名）。
    去掉標點、空白、底線、大小寫，與截斷留下的 …，剩下的是卡名的開頭（含整個相同）→ True；空字串 → False。"""
    t = _NAME_ECHO_STRIP_RE.sub("", (title or "").rstrip("…")).lower()
    return bool(t) and _NAME_ECHO_STRIP_RE.sub("", name or "").lower().startswith(t)


def format_cold_inject_line(
    name: str, raw_content: str, rel_path: str, atom_path: Optional[Path] = None,
    drop_name_echo: bool = False,
) -> str:
    summary = ""
    impression = _extract_named_section(raw_content, "印象")
    if impression:
        for line in impression.split("\n"):
            stripped = line.strip()
            if not stripped or stripped.startswith("##") or stripped.startswith(">"):
                continue
            if stripped.startswith("- "):
                summary = stripped[2:].strip()
            else:
                summary = stripped
            if summary:
                break
    if not summary:
        for line in raw_content.split("\n"):
            if line.startswith("# ") and not line.startswith("## "):
                summary = line[2:].strip()
                break
    if not summary:
        summary = name

    summary = summary.replace("\n", " ").replace("\r", " ").strip()
    if len(summary) > _COLD_LINE_CAP:
        summary = summary[:_COLD_LINE_CAP].rstrip() + "…"

    display_path = pointer_path(atom_path, rel_path, name)
    status = atom_status_suffix(raw_content)
    if drop_name_echo and title_echoes_name(summary, name):
        return f"[Atom:{name}] (cold){status} (full: Read {display_path})"
    return f"[Atom:{name}] (cold) {summary}{status} (full: Read {display_path})"


def load_atoms_within_budget(
    matched: List[AtomEntry],
    memory_dir: Path,
    budget_tokens: int,
    already_injected: List[str],
) -> Tuple[List[str], List[str], int]:
    lines: List[str] = []
    injected: List[str] = []
    used = 0

    for name, rel_path, triggers in matched:
        if name in already_injected:
            continue
        atom_path = (memory_dir / rel_path) if rel_path else (memory_dir / f"{name}.md")
        if not atom_path.exists():
            continue
        try:
            content = atom_path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue

        content = _strip_atom_for_injection(content)
        content_tokens = _estimate_tokens(content)
        if used + content_tokens <= budget_tokens:
            lines.append(f"[Atom:{name}]\n{content}")
            injected.append(name)
            used += content_tokens
        else:
            first_line = content.split("\n", 1)[0].strip("# ").strip()
            lines.append(f"[Atom:{name}] {first_line} (full: Read {pointer_path(atom_path)})")
            injected.append(name)
            break

    return lines, injected, used


# ─── Sub-agent Injection Orchestrator ──────────────────────────
# 可重用注入 orchestrator：parent → sub-agent 記憶注入（PreToolUse updatedInput）。
# 包裝既有純函式（parse_memory_index / match_triggers / bm25_match /
# load_atoms_within_budget / _strip_atom_for_injection）。全域層 only，
# 不依賴 session state["atom_index"]。緊湊 top-k（印象式 strip）守 token 紅線。

SUBAGENT_INJECT_MARKER = "[WG:SubagentMemory]"


_SUBAGENT_TOP_K = 3


def build_injection_blob(
    prompt_str: str,
    *,
    budget: int,
    already_injected: Optional[List[str]] = None,
) -> Tuple[str, List[str]]:
    """為 sub-agent prompt 組緊湊記憶注入 blob。

    回傳 (blob_str, injected_names)。無匹配時回 ("", [])（caller 應保留原 prompt 不改）。
    冪等：若 prompt 已帶本 marker（巢狀 sub-agent）→ 回 ("", [])，不重複注入。

    blob 內含可解析 header `[WG:SubagentMemory] ... atoms=a,b,c`，
    供 PostToolUse 無狀態回推注入清單（不靠 PreToolUse 跨進程關聯）。
    """
    if not prompt_str or SUBAGENT_INJECT_MARKER in prompt_str:
        return "", []

    already = list(already_injected or [])
    entries = parse_memory_index(MEMORY_DIR)
    if not entries:
        return "", []
    # 被取代的舊卡不進子代理注入池（與 UPS 同一條有效性規則）
    superseded = superseded_names_cached(MEMORY_DIR)
    if superseded:
        entries = [e for e in entries if e[0] not in superseded]
    base_dir = MEMORY_DIR.parent  # rel_path 相對 ~/.claude（含 memory/ 與 _AIDocs/ 前綴）

    # 1) trigger 關鍵字匹配
    matched: List[AtomEntry] = [
        e for e in match_triggers(prompt_str, entries) if e[0] not in already
    ]

    # 2) trigger 命中少（≤2）時用 BM25 補（鏡像 UPS 全域層路徑）
    if len(matched) <= 2:
        seen = {e[0] for e in matched}
        try:
            from wg_core import load_config
            _bm25_ms = float(
                (load_config().get("vector_search") or {})
                .get("bm25_min_score", BM25_MIN_SCORE_DEFAULT)
            )
        except Exception:
            _bm25_ms = BM25_MIN_SCORE_DEFAULT
        for entry in bm25_match(prompt_str, entries, min_score=_bm25_ms, top_k=_SUBAGENT_TOP_K):
            if entry[0] not in seen and entry[0] not in already:
                matched.append(entry)
                seen.add(entry[0])

    if not matched:
        return "", []

    # 3) ACT-R activation 排序，緊湊 top-k
    def _act_key(entry: AtomEntry) -> float:
        name, rel_path, _triggers = entry
        atom_dir = (base_dir / rel_path).parent if rel_path else (base_dir / "memory")
        return compute_activation(name, atom_dir)

    matched.sort(key=_act_key, reverse=True)
    matched = matched[:_SUBAGENT_TOP_K]

    # 4) budget 內載入（內部走 _strip_atom_for_injection 印象式 strip）
    lines, injected, _used = load_atoms_within_budget(
        matched, base_dir, budget, already,
    )
    if not injected:
        return "", []

    header = i18n.t("hook.wg_atoms_budget.subagent_header", marker=SUBAGENT_INJECT_MARKER, atoms=','.join(injected))
    body = "\n\n".join(lines)
    blob = f"{header}\n\n{body}\n\n{i18n.t('hook.wg_atoms_budget.subagent_footer')}"
    return blob, injected
