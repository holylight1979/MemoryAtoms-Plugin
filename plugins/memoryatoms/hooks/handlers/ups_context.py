"""
handlers/ups_context.py — UserPromptSubmit context build 段

從 user_prompt_submit.py 拆出（複雜度熱點重構）。
職責：atom 注入前的場景脈絡建構：
- wisdom engine situation classification
- parallel agent suggestion
- _AIDocs keyword matching
- JIT internal pipeline reference（記憶系統開發場景）

公開函式 build_context() 回傳 budget：插件要求保留的量扣掉後的餘額（沒有插件要求就原樣）。
"""

import sys
from pathlib import Path
from typing import Any, Dict, List

from wg_core import user_typed_text
from wg_atoms import count_trigger_hits
import wg_plugins

# 確保本樹根在 path：這個回合後面的 lib.* 惰性 import 靠它（handler 執行時 cwd 不定；用檔案位置不用 Path.home()）。
_LIB_PARENT = str(Path(__file__).resolve().parents[2])
if _LIB_PARENT not in sys.path:
    sys.path.insert(0, _LIB_PARENT)


def build_context(
    session_id: str,
    state: Dict[str, Any],
    config: Dict[str, Any],
    prompt: str,
    clean_prompt: str,
    prompt_lower: str,
    budget: int,
    lines: List[str],
) -> int:
    """執行 context build 段。mutate state / append lines，回傳調整後 budget。"""
    # ─── 插件的提示與統計：prompt 只給使用者打的字（通知、同伴訊息不觸發）；query 是這句拿去檢索的文字 ───
    # 插件回 {"lines": [...], "reserve_tokens": n}＝送這些行，並從這句的預算保留 n（下限 500）
    # 插件拿到的 state 已帶這則的 turn_intent 與 tech_cards_full（UPS 在脈絡段之前算好，wg_core.tech_cards_full）：
    # 只在開發時才送的參考資料，tech_cards_full 為假就不送、也不記已送
    reserve = 0
    cwd = str((state.get("session") or {}).get("cwd") or "")
    for item in wg_plugins.emit("UserPromptSubmit", state=state, config=config, session_id=session_id,
                                prompt=user_typed_text(clean_prompt), query=prompt, cwd=cwd):
        if isinstance(item, dict):
            lines.extend(item.get("lines") or [])
            reserve += int(item.get("reserve_tokens") or 0)
        else:
            lines.append(item)
    if reserve:
        budget = max(budget - reserve, 500)

    # ─── _AIDocs keyword matching ──────────────────
    # 只在命中明顯（關鍵字命中數 ≥ aidocs.min_prompt_hits，預設 2）才指、最多 aidocs.max_prompt_matches（預設 1）條、
    # 命中最多的優先；同一份文件一個 session 只指一次（aidocs.notice_once 沒寫＝開，不看寫碼守門總開關）；超過 5 份命中不送照舊判
    aidocs_cfg = config.get("aidocs", {})
    aidocs_state = state.get("aidocs", {})
    aidocs_kw_map = aidocs_state.get("keywords", {})
    max_matches = aidocs_cfg.get("max_prompt_matches", 1)
    min_hits = aidocs_cfg.get("min_prompt_hits", 2)
    if aidocs_kw_map and prompt.strip():
        scored = [(count_trigger_hits(keywords, prompt_lower), fname) for fname, keywords in aidocs_kw_map.items()]
        scored = [(n, f) for n, f in scored if n >= min_hits]
        scored.sort(key=lambda t: -t[0])
        matched_docs: List[str] = [f for _n, f in scored]
        if matched_docs and len(matched_docs) <= 5 and aidocs_cfg.get("notice_once", True):
            pointed = state.setdefault("aidocs_pointed", [])
            matched_docs = [d for d in matched_docs if d not in pointed]
            pointed.extend(matched_docs[:max_matches])
        if matched_docs and len(matched_docs) <= 5:
            aidocs_root = aidocs_state.get("project_root", "")
            pointer_lines = ["[Guardian:AIDocs] Relevant project docs:"]
            for doc in matched_docs[:max_matches]:
                desc = ""
                for f, d in aidocs_state.get("entries", []):
                    if f == doc:
                        desc = d
                        break
                doc_path = f"_AIDocs/{doc}" if aidocs_root else doc
                pointer_lines.append(f"  → Read `{doc_path}` — {desc[:80]}")
            lines.extend(pointer_lines)

    return budget
