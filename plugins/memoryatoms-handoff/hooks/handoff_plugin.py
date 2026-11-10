"""handoff_plugin.py — 自動交接單掛到核心的插件點（單子內容與判法在 wg_handoff.py）。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

from wg_core import _now_iso, resolve_staging_dir
from i18n import t  # lib/（wg_core 已放進 sys.path）
from wg_handoff import build_handoff_stub, should_write_stub, token_warn_payload


def _write_stub(state: Dict[str, Any], cwd: str, staging: Path, stub_name: str) -> bool:
    if not should_write_stub(staging, state, stub_name):
        return False
    staging.mkdir(parents=True, exist_ok=True)
    (staging / stub_name).write_text(build_handoff_stub(state, cwd), encoding="utf-8", newline="\n")
    state["handoff_stub_path"] = str(staging / stub_name)
    state["handoff_stub_at"] = _now_iso()
    return True


def on_pre_compact(ctx: Dict[str, Any]) -> None:
    """Layer 2：壓縮前自動備六區塊交接（核心保底）。壓縮真的發生＝最可靠信號，不依賴 token 量測。
    客觀區塊自動填、主觀區塊留 TODO，由 Layer 3（handoff_compact.on_post_tool_batch）注入提示叫模型補全；
    postbatch_emit 關就不立旗標（旗標只代表「下一批要送提示」，不留送不出去的）。"""
    state, config = ctx["state"], ctx["config"]
    ah = config.get("auto_handoff", {}) or {}
    if not (ah.get("enabled", True) and ah.get("precompact_stub", True)):
        return
    try:
        cwd = state.get("session", {}).get("cwd", "") or ctx["input_data"].get("cwd", "")
        staging = resolve_staging_dir(cwd)
        stub_name = ah.get("stub_filename", "next-phase-auto.md")
        if _write_stub(state, cwd, staging, stub_name):
            state["pending_handoff_emit"] = bool(ah.get("postbatch_emit", True))
            print(f"[auto-handoff] stub written: {staging / stub_name}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"[auto-handoff] precompact stub error: {e}", file=sys.stderr)


def on_session_end(ctx: Dict[str, Any]) -> None:
    """Layer 4：補「PreCompact 沒觸發、session 直接結束」缺口。有未完成工作且無既有 handoff 時寫客觀 stub 供下個
    session /continue；已無壓縮上下文壓力，只填客觀區塊、主觀照 TODO 佔位（不設 pending_handoff_emit，已無 PostToolBatch 可消費）。
    核心在這之後才數暫存檔，所以這張單會算進暫存檔提示。"""
    state, config = ctx["state"], ctx["config"]
    ah = config.get("auto_handoff", {}) or {}
    if not (ah.get("enabled", True) and ah.get("sessionend_fallback", True)):
        return
    staging = ctx["staging_dir"]
    try:
        stub_name = ah.get("stub_filename", "next-phase-auto.md")
        if _write_stub(state, ctx["cwd"], staging, stub_name):
            print(f"[auto-handoff] sessionend fallback stub: {staging / stub_name}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"[auto-handoff] sessionend fallback error: {e}", file=sys.stderr)


def block_note(ctx: Dict[str, Any]) -> Optional[str]:
    """Layer 1：context 快滿預警，附在收尾擋訊息尾巴（門檻見 wg_handoff.token_warn_payload）。"""
    return token_warn_payload(ctx["state"], ctx["config"], ctx["transcript"], transcript_text=ctx["transcript_text"])


def on_intent(ctx: Dict[str, Any]) -> Optional[str]:
    if ctx["intent"] != "handoff":
        return None
    return t("ext.handoff.intent_detected")
