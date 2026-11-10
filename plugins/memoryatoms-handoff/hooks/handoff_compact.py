"""handoff_compact.py — 壓縮後的兩件交接事，掛核心的插件點 PostCompact、PostToolBatch（單子怎麼寫在 wg_handoff.py）。

- PostCompact：輸入的 compact_summary 寫進這一輪 PreCompact 剛寫的自動交接單（handoff_stub_at 不早於 pre_compact_snapshot）的
  「壓縮前脈絡」段：只換 <!-- compact-summary:begin／end --> 記號之間，沒有就插在「前置脈絡」標題前，其他內容原樣；
  這一輪沒寫自動交接單（沒改檔、或有新鮮的手寫交接單）或摘要是空的就不寫，手寫交接單不碰。
- PostToolBatch：PreCompact 備了單（pending_handoff_emit）→ 下一批工具結果附一次補全提示；摘要已填就送短版。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

from wg_core import _now_iso
import i18n   # lib/（wg_core 已放進 sys.path）：給人看的提示走語言表

SUMMARY_BEGIN = "<!-- compact-summary:begin -->"   # 交接單裡壓縮摘要那段的頭尾記號：只換記號之間，其他內容不動
SUMMARY_END = "<!-- compact-summary:end -->"
SUMMARY_ANCHOR = "## 1.【前置脈絡】"   # 摘要段插在這個標題前（wg_handoff.build_handoff_stub 的第一段）；找不到就接在檔尾
TODO_MARK = "TODO(模型補全)"   # wg_handoff.build_handoff_stub 留給模型補的格子；提示原樣引用（比對記號，不翻）


def fill_compact_summary(state: Dict[str, Any], summary: str, trigger: str = "") -> str:
    """壓縮摘要寫進這一輪 PreCompact 剛寫的自動交接單。只寫記號之間那段：已有就換新、沒有就插在「前置脈絡」標題前，檔裡其他內容原樣。
    這一輪沒寫自動交接單就不寫：手寫的可能是別的 session 的，自動交接單的修改時間一動也會把開場「先讀最新交接檔」的指標搶過來。
    回寫入的檔路徑，沒寫回空字串。"""
    summary = (summary or "").strip()
    stub = state.get("handoff_stub_path") or ""
    stub_at = state.get("handoff_stub_at") or ""
    snap = state.get("pre_compact_snapshot") or ""
    if not summary or not stub or not snap or stub_at < snap:
        return ""
    path = Path(stub)
    if not path.is_file():
        return ""
    how = f"（{trigger}）" if trigger else ""
    block = (
        f"{SUMMARY_BEGIN}\n"
        "## 0.【壓縮前脈絡】（壓縮摘要原文）\n"
        f"> 來源：PostCompact 的 compact_summary{how}，{_now_iso()}。壓縮時模型寫的對話摘要，屬推論；"
        "動工前對照檔案與 git 核實。\n\n"
        f"{summary}\n"
        f"{SUMMARY_END}\n"
    )
    text = path.read_text(encoding="utf-8")
    b, e = text.find(SUMMARY_BEGIN), text.find(SUMMARY_END)
    if b >= 0 and e > b:
        tail = text[e + len(SUMMARY_END):]
        text = text[:b] + block + (tail[1:] if tail.startswith("\n") else tail)
    elif SUMMARY_ANCHOR in text:
        i = text.index(SUMMARY_ANCHOR)
        text = text[:i] + block + "\n" + text[i:]
    else:
        text = text.rstrip("\n") + "\n\n" + block
    path.write_text(text, encoding="utf-8", newline="\n")
    return str(path)


def on_post_compact(ctx: Dict[str, Any]) -> None:
    """插件點 PostCompact：把輸入的壓縮摘要填進這一輪的自動交接單；寫了、或寫壞了都 stderr 一行。"""
    data = ctx.get("input_data") or {}
    try:
        written = fill_compact_summary(ctx["state"], data.get("compact_summary", ""), data.get("trigger", ""))
        if written:
            print(i18n.t("ext.handoff.compaction_summary_written", written=written), file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(i18n.t("ext.handoff.compaction_summary_failed", etype=type(e).__name__, error=e), file=sys.stderr)


def _stub_has_compact_summary(stub_path: str) -> bool:
    """交接單裡已有 PostCompact 寫的壓縮摘要段；讀不到當沒有、stderr 一行。"""
    if not stub_path:
        return False
    try:
        return SUMMARY_BEGIN in Path(stub_path).read_text(encoding="utf-8")
    except OSError as e:
        print(i18n.t("ext.handoff.cannot_read_stub", error=e), file=sys.stderr)
        return False


def on_post_tool_batch(ctx: Dict[str, Any]) -> Optional[str]:
    """插件點 PostToolBatch：PreCompact 備了單 → 這一批附一次補全提示（摘要已填就送短版）。一次性：旗標看到就清。"""
    state, config = ctx["state"], ctx["config"]
    if not state.get("pending_handoff_emit"):
        return None
    state["pending_handoff_emit"] = False
    ah = config.get("auto_handoff", {}) or {}
    if not (ah.get("enabled", True) and ah.get("postbatch_emit", True)):
        return None
    stub_path = state.get("handoff_stub_path", "") or "(staging)"
    if _stub_has_compact_summary(state.get("handoff_stub_path", "")):
        return i18n.t("ext.handoff.summary_filled", stub_path=stub_path, todo=TODO_MARK)
    return i18n.t("ext.handoff.stub_prepared", stub_path=stub_path, todo=TODO_MARK)
