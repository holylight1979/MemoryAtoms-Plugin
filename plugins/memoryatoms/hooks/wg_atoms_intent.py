"""
wg_atoms_intent.py — 意圖分類與主題追蹤：解析 _AIDocs 索引、抽關鍵詞、判斷提問意圖、更新主題追蹤器。

誰叫它：ups_context 等注入前處理。
純搬移自 wg_atoms.py，函式本體未改；hooks/wg_atoms.py 仍 re-export 這裡的名稱。
"""

import re
from typing import Any, Dict, List, Tuple
from pathlib import Path
from wg_core import sanitize_harness_noise
from wg_atoms_match import _kw_match


# ─── _AIDocs Index Parsing ──────────────────────────────────────────────────

AiDocsEntry = Tuple[str, str, List[str]]


def parse_aidocs_index(project_root: Path) -> List[AiDocsEntry]:
    """Parse _AIDocs/_INDEX.md table."""
    index_path = project_root / "_AIDocs" / "_INDEX.md"
    if not index_path.exists():
        return []
    try:
        text = index_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return []

    entries: List[AiDocsEntry] = []
    in_table = False
    for line in text.splitlines():
        stripped = line.strip()
        if not in_table:
            if stripped.startswith("| #") or stripped.startswith("|#"):
                in_table = True
                continue
        else:
            # 表內容忍（同 _parse_trigger_table，silent-failure direction 1）：空行 skip
            # 不結束表；重複表頭 skip。僅「非空且非 |」真內容才視為表結束。
            if stripped == "":
                continue
            if stripped.startswith("| #") or stripped.startswith("|#"):
                continue
            if stripped.startswith("|---") or stripped.startswith("| ---"):
                continue
            if not stripped.startswith("|"):
                in_table = False
                continue
            cells = [c.strip() for c in stripped.split("|") if c.strip()]
            if len(cells) >= 3:
                fname = cells[1].strip("[]() ")
                link_match = re.match(r"\[([^\]]+)\]", cells[1])
                if link_match:
                    fname = link_match.group(1)
                desc = cells[2]
                if fname.startswith("~~") or "淘汰" in desc:
                    continue
                keywords: List[str] = []
                if len(cells) >= 4 and cells[3].strip():
                    keywords = list(dict.fromkeys(k.strip().lower() for k in cells[3].split(",") if k.strip()))
                entries.append((fname, desc, keywords))
    return entries


def extract_aidocs_keywords(entries: List[AiDocsEntry]) -> Dict[str, List[str]]:
    STOP = {"的", "與", "和", "等", "個", "含", "—", "md", "分析", "說明", "文件", "專案"}
    result: Dict[str, List[str]] = {}
    for fname, desc, explicit_kw in entries:
        if explicit_kw:
            result[fname] = explicit_kw[:15]
        else:
            words = re.findall(r"[一-鿿]{2,}|[a-zA-Z_]{3,}", desc.lower())
            keywords = [w for w in words if w not in STOP]
            stem = Path(fname).stem.lower().replace("_", " ").replace("-", " ")
            keywords.extend(stem.split())
            result[fname] = list(set(keywords))[:10]
    return result


# ─── Intent Classifier (was wg_intent.classify_intent) ──────────────────────

# 十個 intent 鍵固定（介面凍結；wg_overview.INTENT_KEYS 對拍）：debug 最前，平手取先鍵（max 取插入序第一個）
INTENT_PATTERNS = {
    "debug": ["crash", "error", "bug", "失敗", "壞", "exception", "為什麼",
              "why", "問題", "traceback", "報錯", "修復", "fix", "不掉血", "沒反應", "閃退",
              "broken", "not working", "doesn't work", "does not work", "failing", "failed",
              "stack trace", "regression", "freezes", "crashes", "segfault"],
    "feature": ["新增", "加功能", "新功能", "加一個", "feature", "實作", "implement",
                "支援", "加上", "擴充", "新加", "多一個",
                "new feature", "add support", "add a new", "add an option", "add a flag", "extend"],
    "refactor": ["重構", "refactor", "整理", "抽出", "拆開", "搬到", "改寫法", "去重",
                 "簡化", "cleanup", "rename", "改名", "合併成",
                 "clean up", "restructure", "split into", "deduplicate", "simplify", "rewrite"],
    "perf": ["效能", "太慢", "很慢", "卡頓", "優化", "perf", "performance", "latency",
             "記憶體", "memory leak", "吃 cpu", "耗時", "加速", "快一點", "fps",
             "slow", "slower", "too slow", "optimize", "optimise", "speed up", "bottleneck", "throughput"],
    "new_project": ["立案", "新專案", "從零", "起專案", "開新專案", "new project",
                    "scaffold", "bootstrap", "初始化專案", "建專案", "新開一個專案",
                    "from scratch", "new repo", "start a new project", "create a new project"],
    "build": ["build", "deploy", "建置", "部署", "安裝", "install", "啟動",
              "setup", "config", "設定", "配置", "環境",
              "compile", "configure", "configuration", "environment", "dependencies", "docker", "pipeline"],
    "design": ["設計", "架構", "design", "architecture", "planning", "方案",
               "proposal", "trade-off", "tradeoff", "tradeoffs", "system design"],
    "recall": ["之前", "上次", "記得", "決策", "決定", "為什麼選",
               "remember", "previous", "history",
               "last time", "we decided", "did we decide", "why did we", "previously", "what did we"],
    "handoff": ["下 session", "下次繼續", "交接", "續接", "下一個 session",
                "resume prompt", "給下次", "next-phase", "handoff", "下個 claude",
                "next session", "hand off", "hand-off", "pick up where"],
}


def classify_intent(prompt: str) -> str:
    """Rule-based intent classifier. Zero LLM overhead (~1ms)。

    _kw_match：ASCII 詞 word-boundary（防 "fix" 誤中 "prefix" 類子字串）、
    CJK 維持子字串比對。
    """
    prompt_lower = prompt.lower()
    scores = {}
    for intent, keywords in INTENT_PATTERNS.items():
        scores[intent] = sum(1 for kw in keywords if _kw_match(kw, prompt_lower))
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "general"


# ─── Topic Tracker ──────────────────────────────────────────────────────────

_TOPIC_STOP_WORDS = frozenset({
    "this", "that", "with", "from", "have", "been", "will", "what", "when",
    "which", "where", "about", "into", "also", "should", "could", "would",
    "these", "those", "them", "your", "make", "just", "only", "some", "very",
    "here", "there", "then", "than", "more", "most", "like", "each", "want",
    "need", "keep", "does", "done", "doing", "help", "sure", "good", "well",
    "okay", "know", "think", "look", "take", "give", "come", "back", "over",
    "after", "before", "other", "file", "line", "code", "true", "false",
})


def _update_topic_tracker(
    state: Dict[str, Any], prompt: str, intent: str, newly_injected: List[str]
) -> None:
    """Accumulate topic signals in state. Pure CPU, < 1ms, zero network."""
    tracker = state.setdefault("topic_tracker", {
        "intent_distribution": {},
        "prompt_count": 0,
        "first_prompt_summary": "",
        "keyword_signals": [],
    })

    dist = tracker["intent_distribution"]
    dist[intent] = dist.get(intent, 0) + 1
    tracker["prompt_count"] = tracker.get("prompt_count", 0) + 1

    # harness 標籤/hook 殘渣先剔——first_prompt_summary 會進 episodic 摘要（episodic_gen），殘留 <ide_opened_file> 等雜訊會污染它。
    # 首 prompt 若剔完全空（純 IDE 事件），留空讓下一個真 prompt 補位。
    clean_prompt = sanitize_harness_noise(prompt)
    if not tracker.get("first_prompt_summary") and clean_prompt:
        tracker["first_prompt_summary"] = clean_prompt[:200]

    existing_kw = set(tracker.get("keyword_signals", []))
    words = re.findall(r"[a-zA-Z一-鿿]{4,}", clean_prompt)
    for w in words:
        wl = w.lower()
        if wl not in _TOPIC_STOP_WORDS and wl not in existing_kw:
            existing_kw.add(wl)
    sa_config = state.get("_sa_config", {})
    max_kw = sa_config.get("max_keyword_signals", 20)
    tracker["keyword_signals"] = sorted(existing_kw)[:max_kw]
