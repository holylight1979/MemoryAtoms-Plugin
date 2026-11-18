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
SNAPSHOT_STALE_DAYS = 8  # 截圖排程每週跑一次，多一天寬限
SNAPSHOT_PROBLEM_DAYS = 3  # 失敗提示只留這幾天，之後等下週排程重試
_SNAPSHOT_TASK = "Claude-Usage-WeeklySnapshot"  # 與截圖工具的 TASK_NAME 同名
_SNAPSHOT_TOOL = Path(__file__).resolve().parents[1] / "tools" / "usage-snapshot" / "usage_snapshot.py"


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


def _snapshot_advisory(last_run_path) -> list:
    """週用量截圖死人開關 → advisory 行（無需提醒回 []）。

    只看排程跑法（scheduled=True）：手動跑的人當場就看到結果。缺檔＝這台沒用截圖工具，不提示。
    逾 SNAPSHOT_STALE_DAYS 沒更新＝排程沒被叫起；最近一次失敗只在 SNAPSHOT_PROBLEM_DAYS 內提示
    （下週排程會再試；核心的重複警告收合不收這類行，所以靠天數自己收）。
    """
    try:
        if not last_run_path.exists():
            return []
        d = json.loads(last_run_path.read_text(encoding="utf-8"))
        if not d.get("scheduled"):
            return []
        at = datetime.fromisoformat(d.get("at", "")).replace(tzinfo=None)
        age = datetime.now() - at
        if age > timedelta(days=SNAPSHOT_STALE_DAYS):
            return [t("ext.usage-watch.snapshot_stale", days=age.days, last=f"{at:%Y-%m-%d %H:%M}",
                      task=_SNAPSHOT_TASK, tool=_SNAPSHOT_TOOL)]
        if d.get("ok") is not True and age <= timedelta(days=SNAPSHOT_PROBLEM_DAYS):
            if d.get("login_needed") is True:
                reason = t("ext.usage-watch.snapshot_login_needed", tool=_SNAPSHOT_TOOL)
            else:
                reason = " ".join(str(d.get("error") or "").split())[:160] or "?"
            return [t("ext.usage-watch.snapshot_missed", at=f"{at:%m-%d %H:%M}", reason=reason, tool=_SNAPSHOT_TOOL)]
        return []
    except Exception as e:  # noqa: BLE001
        _atom_debug_error("session_start:snapshot_advisory", e)
        return [t("ext.usage-watch.snapshot_last_run_unreadable", tool=_SNAPSHOT_TOOL)]


def on_new_session(ctx: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    # 兩邊各自 try：一邊壞不影響另一邊
    for fn, path in ((_usage_watch_advisory, WORKFLOW_DIR / "usage-watch" / "last-run.json"),
                     (_snapshot_advisory, WORKFLOW_DIR / "usage-snapshots" / "usage-last-run.json")):
        try:
            out.extend(fn(path))
        except Exception as e:  # noqa: BLE001
            _atom_debug_error(f"session_start:{fn.__name__}", e)
    return out
