"""wg_context_usage.py — 量 context 用了多少（收尾閘判「context 還夠不夠把事做完」用；插件也可以來拿）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional

from transcript import parse_record  # lib/ 由 wg_core 放進 sys.path
from wg_core import _estimate_tokens


def _usage_totals_from_text(text: str) -> List[int]:
    """逐 assistant turn 的真實 context 佔用 token（input+cache_creation+cache_read）。

    讀 transcript jsonl 每行的 `message.usage`（API 真的算給這輪的量，已含 system
    prompt / 工具定義 / CLAUDE.md / 注入 atom，故無須再加 base_overhead）。回各輪
    總量 list（依序）；無可解析 usage 回 `[]`（呼叫端 fallback 回 char-proxy）。
    """
    totals: List[int] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or '"usage"' not in line:
            continue
        obj = parse_record(line)  # 壞行、非物件（如 `[]`、`1`）回 None
        if obj is None:
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        u = msg.get("usage")
        if not isinstance(u, dict):
            continue
        total = (
            int(u.get("input_tokens", 0) or 0)
            + int(u.get("cache_creation_input_tokens", 0) or 0)
            + int(u.get("cache_read_input_tokens", 0) or 0)
        )
        if total > 0:
            totals.append(total)
    return totals


def _calibrated_window(peak_observed: int, default_window: int) -> int:
    """自我校準 context 視窗上限（分母），無須知道模型或 [1m] beta。

    物理事實：200k 視窗的 session 在逼近 200k 前 harness 就先 auto-compact，usage
    總量永遠摸不到 200k；故**只要曾觀測到 >200k → 數學上必為 1M 視窗**。未破 200k
    時無從反推、用 `default_window`（預設 1M，貼合常跑 1M 的現實）。取 `max` 確保
    default 調低（如 200k）時仍會在真突破後自動升 1M、不會反而縮小。
    """
    calibrated = 1_000_000 if peak_observed > 200_000 else 0
    return max(int(default_window), calibrated)


def estimate_context_usage(
    transcript_path: Any,
    window_tokens: int = 1_000_000,
    base_overhead: int = 15000,
    *,
    text: Optional[str] = None,
) -> float:
    """context 佔用比率（0.0 起；可能 >1.0）。

    **主路徑**：讀 transcript 每輪 `message.usage` 的真實 token 佔用（input+
    cache_creation+cache_read）。分子 = 最近一輪佔用（= 現在 context 實塞多少）；
    分母 = `_calibrated_window`（自我校準 200k/1M，見該函式）。usage 已含常駐
    開銷，**不另加 base_overhead**。
    **Fallback**（transcript 無任何可解析 usage：全新 session / 讀取失敗 / 舊格式）
    → 退回原 char-proxy（`_estimate_tokens` + base_overhead）/ `window_tokens`。

    ⚠️ **僅觸發信號、非硬決策**：回 `0.0` 表無法量測（transcript 不存在 / 讀取
    失敗 / window 非正）→ 自然落在門檻下、不誤觸發。

    text 給定時（read_transcript_tail 共用尾段）直接用該字串、不再開檔；分子取
    最近一輪 usage 本就在尾段，校準峰值同理（context 單調成長至壓縮點）。
    """
    if window_tokens <= 0:
        return 0.0
    if text is None:
        if not transcript_path:
            return 0.0
        try:
            text = Path(transcript_path).read_text(encoding="utf-8", errors="ignore")
        except (OSError, ValueError):
            return 0.0
    if not text:
        return 0.0
    totals = _usage_totals_from_text(text)
    if totals:
        return totals[-1] / _calibrated_window(max(totals), window_tokens)
    # fallback：無 usage → 原 char-proxy（degrade gracefully、與舊行為一致）
    tokens = _estimate_tokens(text) + max(0, int(base_overhead))
    return tokens / window_tokens
