"""
wg_atoms_render.py — 節錄渲染：把 atom 全文剝成注入用的節錄（去中介資料、抽命名段落、印象段、降級版）。

誰叫它：wg_atoms_budget、wg_atoms_truncate 與外部呼叫端。
從 wg_atoms.py 拆出；hooks/wg_atoms.py 仍 re-export 這裡的名稱。
"""

import re
from typing import List, Optional
from wg_core import _estimate_tokens
import i18n   # lib/：給人看的提示走語言表


# ─── Token Budget & Atom Loading ─────────────────────────────────────────────
# budget 常數/計算已集中 wg_core（見該檔「Token budget 單一來源」註解）；
# compute_token_budget 由上方 import re-export。


_STRIP_META_RE = re.compile(
    r"^- (?:Scope|Type|Trigger|Last-used|Created|Confirmations|ReadHits|Tags|TTL|Expires-at):\s.*$\n?",
    re.MULTILINE,
)


_STRIP_SECTION_RE = re.compile(
    r"^## (?:行動|演化日誌)\s*\n[\s\S]*?(?=^## |\Z)",
    re.MULTILINE,
)


_FRONTMATTER_KEEP_RE = re.compile(
    r"^- (?:Confidence|Trigger|Last-used|Status):\s*.+$",
    re.MULTILINE,
)


_HTML_COMMENT_RE = re.compile(r"^[ \t]*<!--.*?-->[ \t]*\n?", re.MULTILINE | re.DOTALL)


_KNOWLEDGE_CAP_TOKENS_DEFAULT = 200
# 預算有剩時知識段 cap 可放寬：取「剩餘預算的一半」，但不超過預設 cap × 此倍數、不低於預設 cap。
# 只拿一半是留給同回合後面的候選卡，不讓第一張卡吃光預算。
_KNOWLEDGE_CAP_WIDEN_MULT = 2


def _knowledge_cap_for_budget(
    remaining_budget: Optional[int], base_cap: int = _KNOWLEDGE_CAP_TOKENS_DEFAULT,
) -> int:
    """依本回合剩餘預算決定知識段 cap；沒給剩餘預算 → 維持 base_cap。"""
    if remaining_budget is None:
        return base_cap
    return max(base_cap, min(int(remaining_budget) // 2, base_cap * _KNOWLEDGE_CAP_WIDEN_MULT))


def _truncated_mark() -> str:
    """知識段截斷標記的固定開頭（語言表那句 {tokens} 之前的字）：decide_atom_injection 拿它認「被預設 cap 截過」。
    標記跟著目前語言走，不寫死字面。"""
    return i18n.t("hook.wg_atoms_render.truncated").split("{tokens}", 1)[0]


def _extract_named_section(
    content: str, section_title: str, max_tokens: Optional[int] = None,
) -> Optional[str]:
    pattern = re.compile(
        r"^##[ \t]+" + re.escape(section_title) + r"[ \t]*\n([\s\S]*?)(?=^## |\Z)",
        re.MULTILINE,
    )
    m = pattern.search(content)
    if not m:
        return None
    # HTML 註解（`<!-- src: sid#uuid -->` 來源指標等）是給 atom-source 讀的，不進注入（省 token）
    body = _HTML_COMMENT_RE.sub("", m.group(1)).rstrip()
    full = f"## {section_title}\n{body}"

    if max_tokens is None:
        return full

    full_tokens = _estimate_tokens(full)
    if full_tokens <= max_tokens:
        return full

    header = f"## {section_title}\n"
    marker = "\n\n" + i18n.t("hook.wg_atoms_render.truncated", tokens=full_tokens)
    # 依實際 token/char 密度換算截斷字元數（CJK 密度高、chars/token 低）
    chars_per_token = len(full) / full_tokens if full_tokens else 4.0
    target_chars = int(max_tokens * chars_per_token) - len(header) - len(marker)
    if target_chars < 50:
        target_chars = 50
    truncated = body[:target_chars]
    snap = truncated.rfind("\n\n")
    if snap < target_chars * 0.5:
        snap = truncated.rfind("\n")
    if snap > 0:
        truncated = truncated[:snap]
    return f"{header}{truncated.rstrip()}{marker}"


def _detect_atom_type(content: str) -> str:
    has_knowledge = _extract_named_section(content, "知識") is not None
    if has_knowledge:
        return "knowledge_mixed"
    has_impression = _extract_named_section(content, "印象") is not None
    if has_impression:
        return "impression_action"
    return "fallback"


def _extract_title_and_frontmatter(content: str) -> str:
    title_line = ""
    for line in content.split("\n"):
        if line.startswith("# ") and not line.startswith("## "):
            title_line = line.rstrip()
            break

    keep_lines = [m.group(0) for m in _FRONTMATTER_KEEP_RE.finditer(content)]
    parts: List[str] = []
    if title_line:
        parts.append(title_line)
    if keep_lines:
        if title_line:
            parts.append("")
        parts.extend(keep_lines)
    return "\n".join(parts)


def _legacy_strip_atom_for_injection(content: str) -> str:
    content = _STRIP_META_RE.sub("", content)
    content = _STRIP_SECTION_RE.sub("", content)
    content = re.sub(r"\n{3,}", "\n\n", content)
    return content.strip()


def _strip_atom_for_injection(
    content: str, knowledge_cap_tokens: int = _KNOWLEDGE_CAP_TOKENS_DEFAULT,
    remaining_budget: Optional[int] = None,
) -> str:
    """表頭＋印象＋知識（超過 cap 從頭截、尾附原長）＋行動。
    remaining_budget 給了 → 知識段 cap 依剩餘預算放寬（見 _knowledge_cap_for_budget）。"""
    knowledge_cap_tokens = _knowledge_cap_for_budget(remaining_budget, knowledge_cap_tokens)
    atom_type = _detect_atom_type(content)

    if atom_type == "fallback":
        return _legacy_strip_atom_for_injection(content)

    parts: List[str] = []
    header = _extract_title_and_frontmatter(content)
    if header:
        parts.append(header)

    impression = _extract_named_section(content, "印象")
    if impression:
        parts.append(impression)

    if atom_type == "knowledge_mixed":
        knowledge = _extract_named_section(content, "知識", max_tokens=knowledge_cap_tokens)
        if knowledge:
            parts.append(knowledge)

    action = _extract_named_section(content, "行動")
    if action:
        parts.append(action)

    return "\n\n".join(parts).strip()


_FALLBACK_KNOWLEDGE_LINES = 2   # 無「印象」段時保留的知識條數（[固]/[觀] 優先）


_FALLBACK_LINE_CHARS = 160      # 每條節錄上限字元（長條目截尾加 …，避免降級版逼近全文大小）


def _strip_atom_for_injection_impression_only(content: str) -> str:
    """budget fallback 用的最小注入：表頭 + 印象段；無印象段則補知識段前幾條，
    讓降級注入仍帶最低知識量（只剩標題+trigger 等於沒唸卻照樣佔 token）。"""
    parts: List[str] = []
    header = _extract_title_and_frontmatter(content)
    if header:
        parts.append(header)
    impression = _extract_named_section(content, "印象")
    if impression:
        parts.append(impression)
    else:
        knowledge = _extract_named_section(content, "知識")
        if knowledge:
            bullets = [ln for ln in knowledge.splitlines() if ln.lstrip().startswith("- ")]
            ranked = ([b for b in bullets if "[固]" in b or "[觀]" in b]
                      + [b for b in bullets if "[固]" not in b and "[觀]" not in b])
            picked = [
                (b if len(b) <= _FALLBACK_LINE_CHARS else b[:_FALLBACK_LINE_CHARS].rstrip() + "…")
                for b in ranked[:_FALLBACK_KNOWLEDGE_LINES]
            ]
            if picked:
                parts.append("## 知識（節錄）\n" + "\n".join(picked))
    return "\n\n".join(parts).strip()
