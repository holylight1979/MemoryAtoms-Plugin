"""dashboard_plugin.py — 收尾報告的顯示面：報告落檔後看 HUD 窗活著沒，窗死才開窗或補訊號。

窗活著＝HUD 頁對 Node 的 SSE 常駐連線在（beat-status.clients ≥ 1；頁被瀏覽器凍結時連線仍在、視窗關了才斷），
或心跳 age_s < hud_stale_s（退路：舊版 Node 無 clients 欄）。判死原因落 Logs/guard-aec_hud.jsonl；Stop 消費旗標前再查一次。

什麼時候開窗（三個都要成立）：
  1. 儀表板有回應、只是沒有 HUD 頁連著（連線都不通時開窗只會得到空頁：只落 log，回 False 讓核心補訊號）；
  2. 設定 aec.hud_autospawn 開著；
  3. severity 是 notable／real-evasion（routine 靜默入 disk、不打擾）。
環境變數 WG_NO_BROWSER 有值（測試、對帳腳本）一律不開：驗「會開窗」要把 subprocess.Popen 換成假的。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Tuple

from wg_core import _atom_debug_error, append_guard_log
from wg_platform import background_spawn_kwargs  # lib/（wg_core 已放進 sys.path）：背景起子行程的平台分流唯一一份
from i18n import t  # lib/（同上）

SIGNAL_SEVERITIES = ("notable", "real-evasion")
REASON_NO_HUD_CLIENT = "no_hud_client_and_beat_stale"   # 伺服器有回應、只是沒有 HUD 頁連著：只有這時開窗才看得到東西
# 本插件的設定預設值（workflow/config.json 的 aec 段可以蓋）：沒裝本插件就沒有意義，所以不放核心設定
HUD_AUTOSPAWN_DEFAULT = True    # aec.hud_autospawn：窗死且有退避訊號時自動開 HUD 窗
HUD_STALE_S_DEFAULT = 75        # aec.hud_stale_s：心跳幾秒沒更新算死（只在舊版 Node 沒有 clients 欄時才看它）


def _dashboard_port(config: Dict[str, Any]) -> int:
    """和 mcp/index.js 同一順序：WG_DASHBOARD_PORT → 設定 dashboard_port → 3848。"""
    return int(os.environ.get("WG_DASHBOARD_PORT") or (config or {}).get("dashboard_port") or 3848)


def _aec_settings(config: Dict[str, Any]) -> Tuple[bool, int]:
    """回 (hud_autospawn, hud_stale_s)：設定有寫照設定，沒寫用本插件的預設值。"""
    aec_cfg = (config or {}).get("aec", {}) or {}
    return bool(aec_cfg.get("hud_autospawn", HUD_AUTOSPAWN_DEFAULT)), int(aec_cfg.get("hud_stale_s", HUD_STALE_S_DEFAULT))


def _hud_alive(port: int, threshold_s: int) -> Tuple[bool, Dict[str, Any]]:
    """GET /api/aec/beat-status → HUD 窗活著？回 (alive, info)。

    活著 = clients ≥ 1（HUD 頁的 SSE 常駐連線在：視窗開著，即使頁面被瀏覽器凍結/節流）
        或 age_s < threshold（心跳新；舊版 Node 無 clients 欄時的退路）。
    心跳是「頁面正在渲染」的證據，Edge --app 視窗被遮住久了會停（瀏覽器休眠/凍結隱藏頁；現場實證窗開著 age 仍上百秒），
    單靠它會把開著的窗判死；連線才是「窗開著」的直接證據。
    info 一律帶實際用的 port/threshold/耗時、查到的 age_s/clients、失敗原因——呼叫端落 guard log
    （可觀測性鐵律：不可達的原因不得吞掉）。不可達 / 舊碼 404 / 逾時 → (False, info)。"""
    import urllib.request
    info: Dict[str, Any] = {"port": port, "threshold_s": threshold_s}
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/aec/beat-status", timeout=0.6
        ) as r:
            data = json.loads(r.read().decode("utf-8"))
        age = int(data.get("age_s", 10 ** 9))
        clients = int(data.get("clients", 0))
        info.update(age_s=age, clients=clients)
        alive = clients >= 1 or age < threshold_s
        info["reason"] = "" if alive else REASON_NO_HUD_CLIENT
    except Exception as e:  # 逾時 / 連線拒絕 / 404 / 非 JSON：全記原因，不分類吞掉
        alive = False
        info["reason"] = f"{type(e).__name__}: {e}"[:200]
    info["elapsed_ms"] = round((time.perf_counter() - t0) * 1000)
    return alive, info


def _find_edge() -> str:
    """定位 msedge 執行檔（僅 Windows 主環境；找不到回 ""）。"""
    if sys.platform == "win32":
        for c in (
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ):
            if Path(c).exists():
                return c
    return ""


def _spawn_hud_edge(port: int) -> None:
    """no-shell spawn Edge --app 開 HUD（config gate 已過）。全庫唯一 browser 外呼，
    刻意 shell=False（AV 安全，比照 mcp/index.js 純 Node 設計）。Fail-open。"""
    if os.environ.get("WG_NO_BROWSER"):     # 測試、對帳腳本：不准碰使用者的桌面
        return
    edge = _find_edge()
    if not edge:
        return
    url = f"http://127.0.0.1:{port}/aec/hud"
    bg: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        **background_spawn_kwargs(),
    }
    try:
        subprocess.Popen([edge, f"--app={url}"], shell=False, **bg)
    except (OSError, ValueError) as e:
        _atom_debug_error("post_tool_use:aec_spawn_edge", e)


def on_report_saved(ctx: Dict[str, Any]) -> bool:
    """AecReport.saved：窗活著（HUD 頁連線在，或心跳新）→ 回 True，會輪詢渲染、無需補訊號。窗死 → 回 False 由核心標旗，
    開不開窗看檔頭那三條。判死原因一律落 guard-aec_hud.jsonl。Fail-open。"""
    sev, config = ctx["severity"], ctx["config"]
    try:
        autospawn, stale_s = _aec_settings(config)
        port = _dashboard_port(config)
        alive, info = _hud_alive(port, stale_s)
        if alive:
            return True
        append_guard_log("aec_hud", {
            "where": "post_tool_use", "session_id": ctx["session_id"], "severity": sev, **info,
        })
        if autospawn and sev in SIGNAL_SEVERITIES and info.get("reason") == REASON_NO_HUD_CLIENT:
            _spawn_hud_edge(port)
    except Exception as e:  # noqa: BLE001
        _atom_debug_error("post_tool_use:aec_maybe_spawn_hud", e)
    return False


def on_report_unseen(ctx: Dict[str, Any]) -> bool:
    """AecReport.unseen：Stop 消費旗標前再查一次（emit 當下不可達可能是暫時的：Node 剛換手、HUD 頁重連中），現在窗活著就不印；
    兩次判定都落 guard-aec_hud.jsonl 可稽核。回 True＝已處理，核心不用再印。"""
    config, sev = ctx["config"], ctx["severity"]
    port = _dashboard_port(config)
    alive, info = _hud_alive(port, _aec_settings(config)[1])
    append_guard_log("aec_hud", {
        "where": "stop", "session_id": ctx["session_id"], "severity": sev, "alive": alive, **info,
    })
    if not alive:
        why = info.get("reason") or t("ext.dashboard.hud_beat_detail", age_s=info.get('age_s'), clients=info.get('clients'))
        print(t("ext.dashboard.hud_unreachable", why=why, report_file=ctx['report_file'], port=port), file=sys.stderr)
    return True
