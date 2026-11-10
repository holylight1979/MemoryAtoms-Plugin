"""
wg_atoms_use.py — 用卡偵測與歸因：從模型回覆判斷哪些卡真的被用到（獨特詞、否定語、引用句、讀檔證據），含向量決勝。

誰叫它：Stop 收尾的回饋歸因、驗證腳本。
純搬移自 wg_atoms.py，函式本體未改；hooks/wg_atoms.py 仍 re-export 這裡的名稱。
"""

import json
import math
import re
from typing import Any, Dict, List, Optional, Tuple
from collections import Counter
from wg_core import MEMORY_DIR, _atom_debug_error
from pathlib import Path
from wg_atoms_compat import iter_atom_files_multi


# ─── Use 偵測：詞彙重疊 ────────────────────────────────────────
# 注入≠使用：某 atom 被注入後是否真的被「用上」，以零成本詞彙重疊判定 —
#   取 atom 的稀有/識別性 token（程式碼識別碼/路徑/API + CJK 雙字 bigram，
#   去停用詞、可選 IDF 過濾高頻 token），與本 turn assistant 訊息＋tool-call args
#   求交集；共享 ≥ rare_token_min 或 containment ≥ overlap_min → 判 used。
#   不確定（差一個）時才用 embedding cosine tiebreak（偶發、fail-safe）。

_USE_CODE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_./:\-]*[A-Za-z0-9_]")


_USE_CJK_RE = re.compile(r"[一-鿿]{2,}")


_USE_EXT_RE = re.compile(r"\.(py|js|json|md|txt|java|ts|tsx|jsx|cjs|mjs|sh|ya?ml)$")


# 去停用詞：英文功能詞 + 域內過泛詞（每個 atom 都會出現 → 無鑑別力）。
_USE_STOPWORDS = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "have", "into", "when",
    "then", "not", "but", "are", "was", "were", "will", "your", "you", "use",
    "used", "using", "uses", "via", "per", "all", "any", "one", "two", "out",
    "get", "set", "add", "new", "old", "see", "may", "can", "其中", "以下",
    # 域內過泛（atom 記憶系統語境）
    "atom", "atoms", "memory", "hook", "hooks", "code", "file", "files", "test",
    "tests", "line", "lines", "state", "config", "data", "true", "false", "none",
    "記憶", "注入", "系統", "原子", "如果", "因為", "所以", "可以", "需要", "問題",
    "功能", "這個", "那個", "沒有", "規則", "處理",
})


def _use_pieces(span: str) -> List[str]:
    """把一段 code-ish span 正規化成可比對 piece（path 段 + _/. 子詞），雙側一致。"""
    out: List[str] = []
    for raw in re.split(r"[/\s:,;()\[\]<>\"'`、，。]+", span.strip().lower()):
        p = raw.strip("._-")
        if not p:
            continue
        p = _USE_EXT_RE.sub("", p)
        if not p:
            continue
        out.append(p)
        for sub in re.split(r"[._]+", p):
            if len(sub) >= 5:
                out.append(sub)
    return out


def extract_distinctive_tokens(text: str) -> set:
    """抽 text 的稀有/識別性 token 集合（雙側同一函式 → 比對一致）。

    - code-ish：含 `_./-`/數字 或 長度≥7 的識別碼/路徑/API（含 path 段與 _ 子詞）
    - CJK：雙字 bigram（去過泛雙字）
    去停用詞、長度<4 丟棄。
    """
    if not text:
        return set()
    toks: set = set()
    for span in _USE_CODE_RE.findall(text):
        for p in _use_pieces(span):
            if len(p) < 4 or p in _USE_STOPWORDS:
                continue
            code_like = bool(re.search(r"[_.\d/\-]", p))
            if code_like or len(p) >= 7 or (5 <= len(p) <= 6):
                toks.add(p)
    for run in _USE_CJK_RE.findall(text):
        for i in range(len(run) - 1):
            bg = run[i:i + 2]
            if bg not in _USE_STOPWORDS:
                toks.add(bg)
    return toks


def build_atom_df(atom_texts: List[str]) -> Tuple[Counter, int]:
    """跨 atom 語料的 document frequency（近似 IDF）：token→出現於幾顆 atom。

    回傳 (df_counter, n_docs)。供 detect_atom_use 過濾「出現在過多 atom」的低鑑別 token。
    """
    df: Counter = Counter()
    n = 0
    for t in atom_texts:
        n += 1
        for tok in extract_distinctive_tokens(t):
            df[tok] += 1
    return df, n


def resolve_atom_path(name: str) -> Optional[Path]:
    """atom name → .md Path（全域層，含 _AIDocs/Failures feedback-*）。找不到回 None。

    僅迭代檔路徑（不讀內容），供 Stop 端解析 sub-agent 注入清單的 atom 名 → 路徑。
    """
    target = f"{name}.md"
    if iter_atom_files_multi is not None:
        try:
            for p in iter_atom_files_multi():
                if p.name == target:
                    return p
        except Exception as e:
            _atom_debug_error("usefulness:atom_lookup_multi", e)
    cand = MEMORY_DIR / target
    return cand if cand.exists() else None


def detect_atom_use(
    atom_content: str,
    turn_text: str,
    *,
    rare_token_min: int = 2,
    overlap_min: float = 0.18,
    df_map: Optional[Counter] = None,
    n_docs: int = 0,
    max_df_ratio: float = 0.5,
    embed_fn=None,
    embed_min: float = 0.62,
) -> Dict[str, Any]:
    """判定 atom 是否在本 turn 被使用。回 {used, shared, containment, method}。

    主判：稀有 token 交集 |shared| ≥ rare_token_min 或 containment ≥ overlap_min。
    IDF 過濾：df_map/n_docs 提供時，丟棄 df/n_docs > max_df_ratio 的過泛 token。
    Tiebreak：差一個（|shared| == rare_token_min-1 且 ≥1）時，若 embed_fn 提供 →
      cosine ≥ embed_min 判 used（method=embed）。embed_fn 失敗回 None → 不影響主判。
    """
    rare = extract_distinctive_tokens(atom_content)
    if df_map is not None and n_docs > 0 and max_df_ratio < 1.0:
        cutoff = max_df_ratio * n_docs
        rare = {t for t in rare if df_map.get(t, 0) <= cutoff}
    if not rare:
        return {"used": False, "shared": 0, "containment": 0.0, "method": "no_rare"}

    turn_tokens = extract_distinctive_tokens(turn_text)
    shared = rare & turn_tokens
    n_shared = len(shared)
    containment = n_shared / len(rare)

    if n_shared >= rare_token_min or containment >= overlap_min:
        return {"used": True, "shared": n_shared, "containment": round(containment, 3),
                "method": "lexical"}

    # tiebreak：差一個才動 embedding（偶發）
    if embed_fn is not None and n_shared == max(0, rare_token_min - 1) and n_shared >= 1:
        try:
            cos = embed_fn(atom_content, turn_text)
        except Exception as e:
            _atom_debug_error("usefulness:embed_tiebreak", e)
            cos = None
        if cos is not None and cos >= embed_min:
            return {"used": True, "shared": n_shared, "containment": round(containment, 3),
                    "method": "embed", "cosine": round(float(cos), 3)}

    return {"used": False, "shared": n_shared, "containment": round(containment, 3),
            "method": "lexical"}


# ─── 判用 v2：行動證據優先、路徑噪音剔除、否定線索 ──────────────────────────────
# 標註集（57 筆）實測 v1 詞彙重疊：precision 0.18、
# 12 組門檻全在 0.17–0.22，rejected／cited 在所有設定下 9/9 判 used——門檻是死路。
# FP 主因：路標／cold 行的路徑片段（users/<帳號>/claude/tools…）幾乎每輪工具參數都有；
# 全文 atom 則是泛雙字。否定與引用靠共享 token 必中，要獨立規則。

def _attr_path_noise() -> frozenset:
    """路徑噪音詞。家目錄那一段是當前使用者的帳號，隨機器而異 → 取用時才問，
    不寫死在版控程式裡（寫死等於只對作者那台機器有效）。"""
    base = {
        "users", "claude", "tools", "aidocs", "memory", "hooks", "workflow", "atoms",
        "memdev", "scratchpad", "appdata", "local", "temp", "python", "utf8", "verify",
        "handlers", "logs", "json", "jsonl", "config", "state", "session", "prompt",
        "atom", "read", "write", "edit", "bash", "file", "path", "line", "lines",
        "test", "tests", "user", "claude.md",
    }
    try:
        from wg_roles import get_current_user
        base.add(get_current_user().lower())
    except Exception:  # noqa: BLE001
        pass
    return frozenset(base)


_ATTR_PATH_NOISE = _attr_path_noise()


# 否定要「綁定到這顆 atom」才算拒用，不是附近有否定詞就算（Codex #8 反例：「遵照 X 完成部署，不要用舊指令」
# 不是拒用 X）。兩種句型：前綴型「不要用／忽略 ＋ ≤15 字 ＋ 錨點」、述語型「錨點 ＋ ≤20 字 ＋ 已過時／被取代／不適用」。
_ATTR_NEG_PREFIX = r"(?:不要用|不用|別用|不採用|不套用|不照|不依|不需要|不能用|忽略|跳過|don'?t use|do not use|skip (?:it|this|that))"


_ATTR_NEG_PRED = r"(?:已過時|過時|已被取代|被取代|不適用|不合用|無關|不對|用不到|deprecated|not applicable|is outdated|superseded)"


_ATTR_DEMONSTRATIVE = r"(?:那|這)(?:顆|條|張|個)\s*(?:atom|卡|規則|條目|記憶)?"


_ATTR_CITE_CUE = r"(?:講的是|說的是|指的是|意思是|內容是|是在說|describes|is about)"


_RESCUE_GENERIC = frozenset({"git push", "git status", "git commit", "git diff", "git log", "git add"})


def _attr_clean_tokens(tokens: set) -> set:
    """去路徑噪音：純路徑段字、含斜線／反斜線／以 ~ 開頭的 token。"""
    out = set()
    for t in tokens:
        tl = t.lower()
        if tl in _ATTR_PATH_NOISE:
            continue
        if "/" in tl or "\\" in tl or tl.startswith("~"):
            continue
        if tl.endswith((".md", ".py", ".js", ".json")) and tl.count(".") == 1 and len(tl) <= 12:
            continue
        out.add(t)
    return out


_ATTR_NAME_PIECE_STOP = frozenset({
    "feedback", "atom", "memory", "workflow", "decisions", "rules", "check", "stdin", "deploy",
    "guard", "index", "config", "state", "hooks", "tools", "skip", "exec", "repo", "mode", "using",
})


def _attr_name_pieces(atom_name: str) -> List[str]:
    """atom slug 拆成可當錨點的片段：使用者常只講「codex-exec 那顆」「上git 那條」。
    片段要 ≥5 字且不在泛詞表——`skip`／`check`／`exec` 這種指令參數常見字當錨點會讓
    `--skip-git-repo-check` 自己命中自己的「否定」規則（實測 5 個測試因此翻紅）。"""
    out: List[str] = []
    for piece in re.split(r"[-_]+", atom_name or ""):
        p = piece.strip().lower()
        if len(p) >= 5 and p not in _ATTR_NAME_PIECE_STOP:
            out.append(p)
    return out


def _rescue_specific(tokens: Optional[List[str]]) -> List[str]:
    """rescue 命中裡「夠特異」的 token：≥8 字、不是路徑（含 / 或 \\ 或磁碟機字母）、非泛 git 指令。
    路徑一律不算：`memory/foo.md`、`C:\\x\\y`、`~/.claude` 每輪工具參數都會出現，不是採用證據。"""
    out: List[str] = []
    for t in tokens or []:
        tl = str(t).strip().lower()
        if len(tl) < 8 or tl in _RESCUE_GENERIC:
            continue
        if "/" in tl or "\\" in tl or re.match(r"^[a-z]:", tl) or tl.startswith("~"):
            continue
        out.append(t)
    return out


_ATTR_SENT_SPLIT_RE = re.compile(r"[。！？!?；;\n]+")


def _attr_anchor_res(atom_name: str) -> List[str]:
    """這顆 atom 的錨點 regex 片段：[Atom:name]、全名、slug 片段（≥4 字）。"""
    parts = [re.escape(f"[atom:{atom_name.lower()}]"), re.escape(atom_name.lower())] if atom_name else []
    parts += [re.escape(p) for p in _attr_name_pieces(atom_name)]
    return [p for p in parts if p]


def _attr_rejected(turn_text: str, atom_name: str, rare_clean: set) -> bool:
    """否定綁定到這顆 atom：
    前綴型  不要用／忽略 …(≤15 字)… 錨點
    述語型  錨點 …(≤20 字)… 已過時／被取代／不適用
    錨點＝[Atom:name]／全名／slug 片段；「那顆 atom／這條」指示詞只在同一句還有 ≥2 個 atom 專屬 token 時才算錨點。"""
    if not turn_text:
        return False
    text_l = turn_text.lower()
    anchors = _attr_anchor_res(atom_name)
    for sent in _ATTR_SENT_SPLIT_RE.split(text_l):
        if not sent.strip():
            continue
        sent_anchors = list(anchors)
        if rare_clean:
            sent_toks = _attr_clean_tokens(extract_distinctive_tokens(sent))
            if len(sent_toks & {t.lower() for t in rare_clean}) >= 2:
                sent_anchors.append(_ATTR_DEMONSTRATIVE)
        if not sent_anchors:
            continue
        anchor_alt = "(?:" + "|".join(sent_anchors) + ")"
        if re.search(_ATTR_NEG_PREFIX + r"[^。；;\n]{0,15}?" + anchor_alt, sent):
            return True
        if re.search(anchor_alt + r"[^。；;\n]{0,20}?" + _ATTR_NEG_PRED, sent):
            return True
    return False


def _attr_cited_sentences(turn_text: str, atom_name: str, rare_clean: set) -> List[str]:
    """回「只是在轉述這顆 atom 內容」的句子（錨點 …(≤12 字)… 講的是／指的是）。"""
    if not turn_text:
        return []
    anchors = _attr_anchor_res(atom_name)
    out: List[str] = []
    for sent in _ATTR_SENT_SPLIT_RE.split(turn_text):
        s_l = sent.lower()
        sent_anchors = list(anchors)
        if rare_clean:
            sent_toks = _attr_clean_tokens(extract_distinctive_tokens(s_l))
            if len(sent_toks & {t.lower() for t in rare_clean}) >= 2:
                sent_anchors.append(_ATTR_DEMONSTRATIVE)
        if not sent_anchors:
            continue
        anchor_alt = "(?:" + "|".join(sent_anchors) + ")"
        if re.search(anchor_alt + r"[^。；;\n]{0,12}?" + _ATTR_CITE_CUE, s_l):
            out.append(sent)
    return out


_READ_EVIDENCE_TMPL = r"(?m)^(?:Read|Bash|Grep|Glob)\s[^\n]*{name}\.md"


def _attr_read_evidence(turn_text: str, atom_name: str) -> bool:
    """本輪真的 Read／cat 過 atom 檔：只認 get_current_turn_text 渲染的工具行（行首 `Read <path>`），
    散文裡提到 `name.md` 不算（Codex #8 反例）。"""
    if not (turn_text and atom_name):
        return False
    return re.search(_READ_EVIDENCE_TMPL.format(name=re.escape(atom_name)), turn_text) is not None


def detect_atom_use_v2(
    atom_content: str,
    turn_text: str,
    *,
    atom_name: str = "",
    form: str = "ok",
    rescue_tokens: Optional[List[str]] = None,
    df_map: Optional[Counter] = None,
    n_docs: int = 0,
    max_df_ratio: float = 0.5,
    shared_min: int = 3,
    containment_min: float = 0.25,
) -> Dict[str, Any]:
    """判定 atom 是否在本 turn 被「採用」。回 {used, method, shared, containment}。

    順序：① 否定線索（atom 名附近有「不要用／已過時／被取代」等）→ rejected，不算 used。
    ② rescue 特異 token 命中（工具參數真的用了 atom 專屬識別）→ used（強證據）。
    ③ 只送一行路標／cold 行且回合沒 Read 該 atom 檔 → 不算 used（沒看到內容不可能採用）。
    ④ 詞彙比對：去路徑噪音與 DF 過泛 token 後，共享 ≥shared_min **且** containment ≥containment_min
       才算；有 Read 過 atom 檔則放寬為共享 ≥2。
    """
    rare_all = extract_distinctive_tokens(atom_content)
    rare_clean_all = _attr_clean_tokens(rare_all)
    if _attr_rejected(turn_text, atom_name, rare_clean_all):
        return {"used": False, "method": "rejected", "shared": 0, "containment": 0.0}
    read_atom = _attr_read_evidence(turn_text, atom_name)
    # 只送路標／cold 行且沒真的 Read 該檔：沒看到內容就不可能採用；rescue 也不算
    #（pointer 化後的 watch token 來自沒送出的全文，Codex #8 反例）。
    if form in ("skip", "cold", "pointer_trim") and not read_atom:
        return {"used": False, "method": "pointer_unread", "shared": 0, "containment": 0.0}
    specific = _rescue_specific(rescue_tokens)
    if specific:
        return {"used": True, "method": "rescue", "shared": len(specific), "containment": 1.0,
                "tokens": specific[:3]}
    # 轉述句（「那顆 atom 講的是…」）不算採用，但轉述之後若有採用證據仍算：把轉述句拿掉再比對
    cited_sents = _attr_cited_sentences(turn_text, atom_name, rare_clean_all)
    text_for_lex = turn_text or ""
    if cited_sents:
        for s in cited_sents:
            text_for_lex = text_for_lex.replace(s, " ")
    rare = set(rare_clean_all)
    if df_map is not None and n_docs > 0 and max_df_ratio < 1.0:
        cutoff = max_df_ratio * n_docs
        rare = {t for t in rare if df_map.get(t, 0) <= cutoff}
    if not rare:
        return {"used": False, "method": ("cited" if cited_sents else "no_rare"), "shared": 0, "containment": 0.0}
    turn_tokens = _attr_clean_tokens(extract_distinctive_tokens(text_for_lex))
    shared = rare & turn_tokens
    n_shared = len(shared)
    containment = n_shared / len(rare)
    need = 2 if read_atom else shared_min
    used = n_shared >= need and (containment >= containment_min or read_atom)
    if not used and cited_sents:
        return {"used": False, "method": "cited", "shared": n_shared, "containment": round(containment, 3)}
    return {"used": bool(used), "method": ("read+lexical" if read_atom else "lexical"),
            "shared": n_shared, "containment": round(containment, 3)}


def make_embed_tiebreak_fn(config: Dict[str, Any]):
    """構造 fail-safe 的 embedding cosine tiebreak callable（或 None）。

    僅當 config.usefulness.embedding_tiebreak 為真才回 callable；任何失敗（服務未起、
    逾時、格式異常）回 None → detect_atom_use 視同無 tiebreak，不污染主判。
    走既有 Ollama /api/embeddings（短逾時、截斷輸入），屬偶發呼叫（僅邊界 case）。
    """
    import urllib.request

    uconf = (config or {}).get("usefulness", {}) or {}
    if not uconf.get("embedding_tiebreak", False):
        return None
    vs = (config or {}).get("vector_search", {}) or {}
    base = vs.get("ollama_base_url", "http://127.0.0.1:11434")
    model = vs.get("embedding_model", "qwen3-embedding")
    timeout_s = float(uconf.get("embed_timeout_s", 1.5))

    def _embed_one(text: str) -> Optional[List[float]]:
        payload = json.dumps({"model": model, "prompt": text[:1500]}).encode("utf-8")
        req = urllib.request.Request(
            f"{base.rstrip('/')}/api/embeddings", data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
        vec = obj.get("embedding")
        return vec if isinstance(vec, list) and vec else None

    def _cosine(a: str, b: str) -> Optional[float]:
        try:
            va, vb = _embed_one(a), _embed_one(b)
            if not va or not vb or len(va) != len(vb):
                return None
            dot = sum(x * y for x, y in zip(va, vb))
            na = math.sqrt(sum(x * x for x in va))
            nb = math.sqrt(sum(y * y for y in vb))
            if na == 0 or nb == 0:
                return None
            return dot / (na * nb)
        except Exception as e:
            _atom_debug_error("usefulness:embed_cosine", e)
            return None

    return _cosine
