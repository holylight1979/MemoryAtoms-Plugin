"""
wg_handoff.py — context 快滿預警（Stop 收尾閘要擋時附在擋訊息尾巴，由 handoff_plugin.block_note 掛上）

- token_warn_payload(state, config, transcript): 純函式決定要不要回預警句（無副作用）。
  量法在核心 wg_context_usage.estimate_context_usage：讀 message.usage 真實 token、200k/1M 分母自我校準，
  沒有 usage 時退回字數估算。預警只叫人主動 /handoff，不替下個 session 備任何東西。
"""

from typing import Any, Dict, Optional

from wg_context_usage import estimate_context_usage
from i18n import t  # lib/（wg_core 已放進 sys.path）


def token_warn_payload(
    state: Dict[str, Any], config: Dict[str, Any], transcript_path: Any,
    *,
    transcript_text: Optional[str] = None,
) -> Optional[str]:
    """預警句決策（**純函式、無副作用**，方便單元測試）。

    回非空字串 = 應 piggyback 到既有 Stop block 的 reason 末尾；回 None = 不警。
    一次性（token_warn_emitted）/ config 開關（enabled、token_warn）/ 門檻
    （token_warn_ratio）全在此判定。**不設旗標**——旗標由呼叫端在「實際 append
    到會 output_block 的 reason」時才標，避免 warning 未顯示就被當成已發。
    """
    ah = config.get("auto_handoff", {}) or {}
    if not ah.get("enabled", True) or not ah.get("token_warn", True):
        return None
    if state.get("token_warn_emitted"):
        return None
    ratio = estimate_context_usage(
        transcript_path,
        ah.get("context_window_tokens", 1_000_000),
        ah.get("context_base_overhead_tokens", 15000),
        text=transcript_text,
    )
    if ratio < float(ah.get("token_warn_ratio", 0.85)):
        return None
    pct = int(ratio * 100)
    return t("ext.handoff.context_nearly_full", pct=pct)
