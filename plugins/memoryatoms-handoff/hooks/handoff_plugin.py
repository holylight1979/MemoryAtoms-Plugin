"""handoff_plugin.py — handoff 擴充掛到核心的插件點：Stop 收尾時的 context 快滿預警、使用者說要交接時提示用 /handoff（預警門檻在 wg_handoff.py）。"""
from __future__ import annotations

from typing import Any, Dict, Optional

import wg_core  # noqa: F401  先載核心：它把 lib/ 放進 sys.path（i18n、wg_handoff 用到的量法都從那裡載）
from i18n import t
from wg_handoff import token_warn_payload


def block_note(ctx: Dict[str, Any]) -> Optional[str]:
    """context 快滿預警，附在收尾擋訊息尾巴（門檻見 wg_handoff.token_warn_payload）。"""
    return token_warn_payload(ctx["state"], ctx["config"], ctx["transcript"], transcript_text=ctx["transcript_text"])


def on_intent(ctx: Dict[str, Any]) -> Optional[str]:
    if ctx["intent"] != "handoff":
        return None
    return t("ext.handoff.intent_detected")
