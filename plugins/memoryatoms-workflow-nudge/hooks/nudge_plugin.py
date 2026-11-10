"""nudge_plugin.py — fan-out／並行建議掛到核心的插件點（判法在 wg_research.py、wg_parallel.py）。"""
from __future__ import annotations

from typing import Any, Dict, Optional

from wg_core import _atom_debug_error
from wg_parallel import detect_parallel_opportunity
from wg_research import detect_research_fanout


def on_prompt(ctx: Dict[str, Any]) -> Optional[str]:
    """先判 research：它的提示已含並行指示，命中就不再建議並行，免得重複佔位。"""
    typed, state, config = ctx["prompt"], ctx["state"], ctx["config"]
    try:
        research_line = detect_research_fanout(typed, state, config)
        if research_line:
            return research_line
    except Exception as e:  # noqa: BLE001
        _atom_debug_error("ResearchFanout", e)
    try:
        return detect_parallel_opportunity(typed, state, config)
    except Exception as e:  # noqa: BLE001
        _atom_debug_error("ParallelSuggest", e)
    return None
