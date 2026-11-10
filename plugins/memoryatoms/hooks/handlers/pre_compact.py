"""
handlers/pre_compact.py — PreCompact hook handler

於 context 壓縮前記下已注入的卡名、備好交接單（壓縮會丟掉對話裡的脈絡）。
"""

import sys
from typing import Any, Dict

from wg_core import _ensure_state, output_nothing, write_state, _now_iso
import wg_plugins


def handle_pre_compact(input_data: Dict[str, Any], config: Dict[str, Any]) -> None:
    session_id = input_data.get("session_id", "")
    state = _ensure_state(session_id, input_data, config)
    if not state:
        output_nothing()
        return

    state["pre_compact_snapshot"] = _now_iso()
    # 快照壓縮前已注入的卡名：SessionStart(compact) 讀它把內文送回去（它跑在 PostCompact 之前、會清空 injected_atoms），
    # 沒送成才由 PostCompact 暫存、PostToolBatch 補位。上一輪壓縮的「已送」記號在這裡清，記號只代表這一輪。
    state["pre_compact_injected_atoms"] = list(dict.fromkeys(state.get("injected_atoms", []) or []))
    state.pop("compact_reinjected_at", None)

    wg_plugins.emit("PreCompact", state=state, config=config, session_id=session_id, input_data=input_data)

    write_state(session_id, state)
    output_nothing()
