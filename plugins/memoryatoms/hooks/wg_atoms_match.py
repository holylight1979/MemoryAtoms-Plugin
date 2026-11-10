"""
wg_atoms_match.py — 比對與排序：ACT-R 活化度與注入排名、觸發詞比對、BM25 全文比對、RRF 融合排序。

誰叫它：候選池與注入管線。
純搬移自 wg_atoms.py，函式本體未改；hooks/wg_atoms.py 仍 re-export 這裡的名稱。
"""

import math
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path
from functools import lru_cache
from wg_atoms_index import AtomEntry, load_access_cached


# 個別化 decay 旋鈕預設（config usefulness.stability_gamma；0=關閉退回固定 d=0.5）
_STABILITY_GAMMA_DEFAULT = 0.3


_DECAY_D_MIN = 0.3


_DECAY_D_MAX = 0.5


def _decay_exponent(access: Dict[str, Any], config: Optional[Dict[str, Any]]) -> float:
    """ACT-R decay 指數 d 的個別化：d = clamp(0.5 − γ·wilson_lb, 0.3, 0.5)。

    被效用閉環證明有用的 atom（Wilson 下界高）衰減更慢（記憶更穩固）。
    無 config（legacy caller）/ γ≤0 / 無效用樣本（n=0）→ 0.5 不變。fail-open。
    """
    if not config:
        return _DECAY_D_MAX
    u = config.get("usefulness") or {}
    try:
        gamma = float(u.get("stability_gamma", _STABILITY_GAMMA_DEFAULT))
    except (TypeError, ValueError):
        return _DECAY_D_MAX
    if gamma <= 0:
        return _DECAY_D_MAX
    try:
        from lib.atom_access import usefulness_stats, WILSON_Z_DEFAULT
        st = usefulness_stats(access, z=float(u.get("wilson_z", WILSON_Z_DEFAULT)))
        if st.get("n", 0) <= 0:
            return _DECAY_D_MAX
        return min(_DECAY_D_MAX, max(_DECAY_D_MIN, _DECAY_D_MAX - gamma * st["lower_bound"]))
    except Exception:
        return _DECAY_D_MAX


def compute_activation(
    atom_name: str, atom_dir: Path,
    config: Optional[Dict[str, Any]] = None,
    access_cache: Optional[Dict[str, Dict[str, Any]]] = None,
) -> float:
    """ACT-R base-level activation: B_i = ln(Σ t_k^{-d})。

    d 預設 0.5；config 給定時走 _decay_exponent 個別化（效用高者衰減慢）。
    無 access log（新 atom / sidecar 缺失）回中性 0.0——不當「最低分」
    讓新 atom 在排序/截斷時優先被犧牲（曝光都還沒開始就被壓死）。
    """
    data = load_access_cached(atom_dir / f"{atom_name}.md", access_cache)
    timestamps = data.get("timestamps") or []
    if not timestamps:
        return 0.0
    d = _decay_exponent(data, config)
    now = time.time()
    total = 0.0
    for ts in timestamps:
        try:
            t_k = max(now - float(ts), 1.0)
        except (TypeError, ValueError):
            continue
        total += t_k ** -d
    return math.log(total) if total > 0 else 0.0


def compute_injection_rank(
    atom_name: str, atom_dir: Path, config: Optional[Dict[str, Any]] = None,
    access_cache: Optional[Dict[str, Dict[str, Any]]] = None,
) -> float:
    """注入排序鍵 = ACT-R activation − 分心懲罰（高曝光低效用者降權）。

    憲法 Context Distraction 對策（_AIDocs/context-memory-governance.md）：read_hits 是
    純曝光、不代表有用；對「已有足夠效用樣本(n≥min_n)但 Wilson 下界低」的 atom 課
    penalty = w·log10(read_hits+1)·(1−lb)，降其注入優先序。
    寧漏勿誤殺：n<min_n（新 atom / 樣本不足）一律不罰；關閉 / 資料缺失 → 退回純
    activation（fail-open）。config usefulness.distraction_{enabled,weight} 旋鈕。
    """
    activation = compute_activation(atom_name, atom_dir, config, access_cache)
    if not config:
        return activation  # 無 config（讀取失敗）→ fail-open 不罰
    u = config.get("usefulness") or {}
    if not u.get("distraction_enabled", True):
        return activation
    weight = float(u.get("distraction_weight", 0.5) or 0.0)
    if weight <= 0:
        return activation
    # 核心策展 atom（decisions/workflow-*/preferences/toolchain/feedback-* ...）豁免
    # distraction penalty。turn-global 歸因下高頻核心 atom 系統性累積無辜 β，penalty 反把最重的
    # 懲罰打在最該注入的人工策展知識上（曝光越高罰越重＝頻率 artifact，與策展價值反相關）＝止血。
    try:
        from lib.atom_locations import is_core_protected_name
        if is_core_protected_name(atom_name):
            return activation
    except Exception:
        pass
    try:
        from lib.atom_access import usefulness_stats, WILSON_Z_DEFAULT
        acc = load_access_cached(atom_dir / f"{atom_name}.md", access_cache)
        read_hits = int(acc.get("read_hits") or 0)
        if read_hits <= 0:
            return activation
        st = usefulness_stats(acc, z=float(u.get("wilson_z", WILSON_Z_DEFAULT)))
        if st.get("n", 0) < int(u.get("min_n", 3)):
            return activation  # 樣本不足不罰（保守，防壓新 atom）
        penalty = weight * math.log10(read_hits + 1) * (1.0 - st.get("lower_bound", 0.0))
        return activation - penalty
    except Exception:
        return activation


@lru_cache(maxsize=4096)
def _kw_pattern(kw: str) -> "re.Pattern":
    """trigger keyword → 編譯後 word-boundary pattern（memoized）。

    全索引 trigger 詞彙量超過 re 模組內建 512 條 pattern cache 上限時，每次
    _kw_match 觸發整包重編譯（實測佔 UPS 主路徑 ~85% CPU）；以本地 lru_cache
    釘住（詞彙量由索引大小自然封頂，4096 綽綽有餘）。
    """
    return re.compile(r'(?<![\w-])' + re.escape(kw) + r'(?![\w-])')


def _kw_match(kw: str, prompt_lower: str) -> bool:
    """Match a trigger keyword against prompt. ASCII uses word-boundary, CJK uses substring."""
    if kw.isascii():
        # 廉價預篩：literal kw 非子字串則 pattern 必不中——絕大多數 keyword 未出現在
        # prompt，免掉 per-kw regex 編譯/搜尋（冷行程全索引 ~千餘詞的編譯是 UPS
        # 主路徑最大 CPU 項）。語意零變：子字串包含是 word-boundary match 的必要條件。
        if kw not in prompt_lower:
            return False
        return bool(_kw_pattern(kw).search(prompt_lower))
    return kw in prompt_lower


def any_trigger_hit(keywords, prompt_lower: str) -> bool:
    """keyword 清單 vs prompt 的單一比對原語（trigger match / AIDocs sweep 共用）。"""
    return any(_kw_match(kw, prompt_lower) for kw in keywords)


def count_trigger_hits(keywords, prompt_lower: str) -> int:
    """命中數版本（RRF trigger 路排序依據）。"""
    return sum(1 for kw in keywords if _kw_match(kw, prompt_lower))


def match_triggers(prompt: str, atoms: List[AtomEntry]) -> List[AtomEntry]:
    prompt_lower = prompt.lower()
    matched = []
    for name, rel_path, triggers in atoms:
        if any_trigger_hit(triggers, prompt_lower):
            matched.append((name, rel_path, triggers))
    return matched


# ─── BM25 Match ─────────────────────────────────────────────────────
# Hand-rolled BM25 over atom trigger lists. ~30 lines, no external dep.
# Use case: global layer (~17 atoms) — replaces vector service round-trip
# (200-500ms) with in-memory <10ms scoring.

_BM25_K1 = 1.2


_BM25_B = 0.75


# min_score 預設單一來源（簽名預設 / UPS fallback / sub-agent blob 三處同值）
# 7.0 由回歸集調參定案：負例誤注入 21.4%→0%、R@3 -1.5pt（漏網由 vector fallback 補位）
BM25_MIN_SCORE_DEFAULT = 7.0


# 請求框架詞的中文 bigram：只表達「我在請你做事」，不帶主題。它們在 atom 文本裡罕見 → IDF 高，
# 兩個就能越過 min_score 7.0（實測「幫我想三個晚餐菜色」命中 workflow-research-fanout、
# 「請你幫我列出五種室內植物」命中 feedback-能自動化實跑…）。查詢與文件兩側都剔除。
_BM25_CJK_STOP = frozenset({
    "幫我", "我想", "請你", "你幫", "幫忙", "麻煩", "請問", "一下", "可以", "可不", "能不", "不能",
    "能夠", "是否", "有沒", "沒有", "怎麼", "什麼", "如何", "這個", "那個", "這樣", "那樣", "我們",
    "你們", "需要", "知道", "想要", "要不", "不要", "應該", "一個", "幾個", "比較", "想知",
})


# 英文虛詞（冠詞、代名詞、助動詞、介系詞、連接詞、疑問詞）：出廠卡多是中文，英文詞 df 極小 → IDF 高，
# 一個虛詞就能把分數推到 7.0 上下（實測「How do I build this project, and …」：trial-and-error 的 and +6.5、
# lost in the middle 的 the +5.9、sed -i 的 i +7.6、add-from-frontmatter 的 from +8.0）。查詢與文件兩側都剔除。
_BM25_EN_STOP = frozenset({
    "a", "an", "the", "and", "or", "but", "if", "then", "so", "nor", "of", "to", "in", "on", "at", "by", "for",
    "with", "from", "into", "onto", "about", "as", "than", "before", "after", "over", "under", "up", "out", "off",
    "is", "are", "was", "were", "be", "been", "being", "am", "do", "does", "did", "doing", "done",
    "have", "has", "had", "can", "could", "would", "should", "will", "shall", "may", "might", "must",
    "i", "me", "my", "we", "us", "our", "you", "your", "he", "she", "it", "its", "they", "them", "their",
    "this", "that", "these", "those", "there", "here", "what", "which", "who", "whom", "whose",
    "when", "where", "why", "how", "not", "no", "yes", "please", "just", "also", "any", "some", "all", "each",
    "very", "too", "again", "only", "same", "both", "other", "such", "let",
})


def _bm25_tokenize(text: str) -> List[str]:
    """Tokenize: ASCII words + Chinese char-bigrams（剔除請求框架 bigram 與英文虛詞）."""
    text = text.lower()
    tokens: List[str] = [w for w in re.findall(r"[a-z0-9]+", text) if w not in _BM25_EN_STOP]
    # Chinese char bigrams (CJK Unified)
    cjk = re.findall(r"[一-鿿]+", text)
    for run in cjk:
        if len(run) == 1:
            tokens.append(run)
        else:
            for i in range(len(run) - 1):
                bg = run[i:i + 2]
                if bg not in _BM25_CJK_STOP:
                    tokens.append(bg)
    return tokens


def _bm25_score(prompt: str, atoms: List[AtomEntry], min_terms: int = 1) -> List[Tuple[str, float]]:
    """Score each atom by BM25 over its trigger list + atom name. Returns sorted (name, score).
    min_terms：至少要有幾個不同的查詢詞命中這張卡才計分（1＝不限）。"""
    if not atoms:
        return []
    # Each atom = one "document" = triggers + name
    docs: List[List[str]] = []
    for name, _rel, triggers in atoms:
        doc_text = " ".join(triggers) + " " + name.replace("-", " ")
        docs.append(_bm25_tokenize(doc_text))

    avgdl = sum(len(d) for d in docs) / max(len(docs), 1)
    N = len(docs)

    # Document frequency
    df: Dict[str, int] = {}
    for d in docs:
        for t in set(d):
            df[t] = df.get(t, 0) + 1

    query_tokens = _bm25_tokenize(prompt)
    if not query_tokens:
        return []

    scored: List[Tuple[str, float]] = []
    for (name, _rel, _triggers), doc in zip(atoms, docs):
        if not doc:
            continue
        dl = len(doc)
        # Term frequency in doc
        tf_doc: Dict[str, int] = {}
        for t in doc:
            tf_doc[t] = tf_doc.get(t, 0) + 1
        if min_terms > 1 and len(set(query_tokens) & tf_doc.keys()) < min_terms:
            continue
        score = 0.0
        for q in set(query_tokens):
            if q not in tf_doc:
                continue
            f = tf_doc[q]
            n_q = df.get(q, 0)
            idf = math.log(1 + (N - n_q + 0.5) / (n_q + 0.5))
            denom = f + _BM25_K1 * (1 - _BM25_B + _BM25_B * dl / avgdl)
            score += idf * (f * (_BM25_K1 + 1)) / max(denom, 1e-9)
        if score > 0:
            scored.append((name, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def bm25_match(
    prompt: str,
    atoms: List[AtomEntry],
    min_score: float = BM25_MIN_SCORE_DEFAULT,
    top_k: int = 3,
    min_terms: int = 1,
) -> List[AtomEntry]:
    """Return top-k atoms whose BM25 score exceeds min_score（min_terms 見 _bm25_score）."""
    scored = _bm25_score(prompt, atoms, min_terms)
    if not scored:
        return []
    by_name = {a[0]: a for a in atoms}
    result: List[AtomEntry] = []
    for name, score in scored[:top_k]:
        if score < min_score:
            break
        if name in by_name:
            result.append(by_name[name])
    return result


# ─── RRF 融合（多路檢索 rank 融合）───────────────────────────────────────────
# 多路（trigger / bm25 / vector）都有結果時，排序以 Reciprocal Rank Fusion 取代
# 「各路各自門檻 + 串接」：score = Σ_routes 1/(k + rank)。只決定排序；各路既有
# min_score（BM25 3.5 / vector 0.65）仍是入場過濾，不因融合放寬。
# config: vector_search.fusion = "rrf"（預設）| "legacy"（回退原排序）。

RRF_K_DEFAULT = 60


# activation（記憶強度）作為融合後排序的乘性調節：final = rrf · exp(gain·rank)。
# gain=0.25 → activation ±2 對應 ×0.61…×1.65 調節——相關性（RRF）為主、
# 記憶強度為輔，不讓 activation 的大值域反客為主。
RRF_ACTIVATION_GAIN = 0.25


def rrf_fuse(
    route_ranked: Dict[str, List[str]], k: int = RRF_K_DEFAULT,
) -> Dict[str, float]:
    """RRF rank 融合：route_ranked = {route: [name 依該路排序]} → {name: score}。

    純函式；rank 1-based（清單首位 rank=1 → 1/(k+1)）。多路命中者分數相加。
    """
    scores: Dict[str, float] = {}
    for names in route_ranked.values():
        for i, nm in enumerate(names):
            scores[nm] = scores.get(nm, 0.0) + 1.0 / (k + i + 1)
    return scores
