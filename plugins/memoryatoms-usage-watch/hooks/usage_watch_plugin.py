"""usage_watch_plugin.py — 用量異常監控的死人開關：開新 session 時提示監控沒在跑或上次有問題。"""
from __future__ import annotations

import getpass
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List

from pathlib import Path

from wg_core import WORKFLOW_DIR, _atom_debug_error
from i18n import t  # lib/（wg_core 已放進 sys.path）

USAGE_WATCH_STALE_HOURS = 72  # 每小時排程；留週末關機的寬限
_TOOL = Path(__file__).resolve().parents[1] / "tools" / "usage-watch.py"  # 提示裡給人複製的指令用實際路徑


def _usage_watch_advisory(last_run_path) -> list:
    """用量異常監控死人開關 → advisory 行（無異常回 []）。

    排程沒被叫起或被刪時腳本不會寫 last-run，只能從逾 USAGE_WATCH_STALE_HOURS 沒更新反推；
    只有排程跑法（scheduled=True，帶 --notify）才算：手動查過一次就不管的人，不該從此每場都被提醒去註冊排程。
    上次查詢失敗、通知跳不出來也浮出（否則異常發生了也沒人知道）。缺檔＝這台沒用這支工具，不提示。
    """
    try:
        if not last_run_path.exists():
            return []
        d = json.loads(last_run_path.read_text(encoding="utf-8"))
        at = datetime.fromisoformat(d.get("at", "")).replace(tzinfo=None)
        if d.get("scheduled") and datetime.now() - at > timedelta(hours=USAGE_WATCH_STALE_HOURS):
            task = f"Claude-Usage-Watch-{getpass.getuser()}"
            return [
                t("ext.usage-watch.monitor_stale", days=(datetime.now() - at).days, last=f"{at:%Y-%m-%d %H:%M}", task=task, tool=_TOOL)
            ]
        problems = ([d.get("error")] if not d.get("ok") else []) + [
            t("ext.usage-watch.notify_failed", channel=ch, error=err) for ch, err in (d.get("notify_errors") or {}).items()]
        if problems:
            return [t("ext.usage-watch.last_run_problem", at=f"{at:%m-%d %H:%M}", problems=t("ext.usage-watch.problem_separator").join(problems))]
        return []
    except Exception as e:  # noqa: BLE001
        _atom_debug_error("session_start:usage_watch_advisory", e)
        return [
            t("ext.usage-watch.last_run_unreadable", tool=_TOOL)
        ]


def on_new_session(ctx: Dict[str, Any]) -> List[str]:
    return _usage_watch_advisory(WORKFLOW_DIR / "usage-watch" / "last-run.json")
