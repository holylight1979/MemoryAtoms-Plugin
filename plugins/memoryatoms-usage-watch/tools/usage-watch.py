#!/usr/bin/env python3
"""usage-watch.py — Claude 用量異常偵測與紀錄：帳號級額度（所有機器／網頁合計）＋本機 transcript 追查是誰在燒。

做什麼：
  【帳號】用 Claude Code 自己的登入 token（~/.claude/.credentials.json）打 api.anthropic.com/api/oauth/usage，
    拿整個帳號的每週％、5 小時窗％與重置時間（只有百分比、沒有 token 數；非公開端點，格式變了會明確報錯）。
    token 8 小時過期（桌面版 Code 分頁不會刷新這個檔）→ 跑一次 `claude auth status` 讓 Claude Code 自己刷新再查；
    不自己打 OAuth 刷新（會輪替 refresh token、弄壞 Claude Code 登入）。
    異常定義：
      ① 突增：與最近一筆同週期、間隔 45 分～3h 的紀錄比，每小時漲 ≥ max(--rate-alert, 歷史漲速中位數×3)
      ② 會提早用完：用最近 24h 速度（不足 12h 退回本週平均）推算用完時間早於重置
      ③ 里程碑：每週到 50／75／90％ 各通知一次
      ④ 5 小時窗 ≥ --five-hour-alert％
      日：今日漲幅 ≥ max(--daily-budget, 過去日用量中位數×2)
      週：比上週同一時間點 ≥ --week-factor 倍且多 10 個百分點以上；另列近幾週結算
  【本機】掃 ~/.claude/projects/**/*.jsonl（含 subagents）的 assistant usage，以 (message.id, requestId) 去重，
    主指標「加權 token」＝ input + cache寫入×1.25(5m)/×2(1h) + cache讀取×0.1 + output×5（貼近計費）。
    每小時超過基線 P95 且 ≥ --hour-factor×中位數 → 異常並列專案／session；每日列最多的專案；每週滾動比前幾週。
  【紀錄】（workflow/usage-watch/，不進版控，供日後圖表）
    account.jsonl  每次查詢一行：at, weekly, weekly_reset, five_hour, five_hour_reset, limits{各額度 utilization}
    alerts.jsonl   每次判定出的異常：at, key, msg, notified
    hourly.json    本機每小時×專案：{小時: {專案: [加權, 原始]}}（同格取大，transcript 被清掉也留得住）
    last-run.json  最近一次執行結果（SessionStart 據此提醒排程沒跑〔只看帶 --notify 的排程跑法〕、查詢或通知失敗）
    settings.json  {"discord_webhook": "..."}（網址等同發文權限，只放本機）
  【通知】--notify 時新異常跳 Windows 通知，settings.json 有 webhook 就同時發 Discord；
    同一種異常在各自冷卻時間內不重發（突增 3h、預估 6h、日／週 24h、里程碑每週一次）。--test-notify 發一則測試。
  【摘要】--report 把帳號段現況（含當下異常）發到 Discord，不論有沒有異常；手動跑，或用 report 排程每天固定時間發。
  【圖表】--chart 從上面的紀錄產生 chart.html（每週額度走勢＋重置／異常標記、每日用量、本機各專案每日 token，
    各附數據表），用瀏覽器打開；不打 API，本機帳本會先掃一次 transcript 更新（--account-only 則跳過）。

怎麼跑：
  python <extensions>/usage-watch/tools/usage-watch.py                 帳號＋本機
  python <extensions>/usage-watch/tools/usage-watch.py --account-only  只查帳號（約 1 秒）
  python <extensions>/usage-watch/tools/usage-watch.py --account-only --report   查帳號並發摘要到 Discord
  python <extensions>/usage-watch/tools/usage-watch.py --chart         產生並打開圖表（workflow/usage-watch/chart.html）
  python <extensions>/usage-watch/tools/usage-watch.py --status        各排程的上次／下次執行與最近一次結果
  python <extensions>/usage-watch/tools/usage-watch.py --register [watch]           註冊每小時監控（--account-only --notify）
  python <extensions>/usage-watch/tools/usage-watch.py --register report --at HH:MM [--on weekdays|mon,wed,fri]
                                                        註冊定時摘要（--account-only --report；預設每天 09:00）
  python <extensions>/usage-watch/tools/usage-watch.py --unregister [watch|report]  移除對應排程（預設 watch）
  完整參數：--help
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import statistics
import subprocess
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

# pythonw（Task Scheduler 靜默跑）下 sys.stdout/stderr 為 None → 導到 devnull。
for _name in ("stdout", "stderr"):
    _s = getattr(sys, _name)
    if _s is None:
        setattr(sys, _name, open(os.devnull, "w", encoding="utf-8", newline="\n"))
    elif hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

_CODE_DIR = Path(os.environ.get("WG_CORE_ROOT") or Path(__file__).resolve().parents[3])   # 程式碼根（extensions/usage-watch/tools/ 往上）：語言表從本樹載
if str(_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(_CODE_DIR))
from lib.i18n import t  # noqa: E402

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", "").strip() or Path.home() / ".claude")   # Claude Code 自己的設定目錄：憑證、逐字稿
DATA_DIR = Path(os.environ.get("WG_CLAUDE_DIR", "").strip() or Path.home() / ".claude")        # 記憶系統資料根：用量紀錄放這
PROJECTS = CLAUDE_DIR / "projects"
OUT_DIR = DATA_DIR / "workflow" / "usage-watch"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # 只有 Windows 有這個旗標；其他平台 0
ACCOUNT_LOG = OUT_DIR / "account.jsonl"
ALERT_LOG = OUT_DIR / "alerts.jsonl"
LAST_RUN = OUT_DIR / "last-run.json"
LEDGER = OUT_DIR / "hourly.json"
SETTINGS = OUT_DIR / "settings.json"
CHART = OUT_DIR / "chart.html"
USAGE_API = "https://api.anthropic.com/api/oauth/usage"
TASKS = {  # 排程種類 → (排程名, 執行參數)；排程名帶 Windows 帳號，每帳號一份
    "watch": (f"Claude-Usage-Watch-{getpass.getuser()}", "--account-only --notify"),
    "report": (f"Claude-Usage-Report-{getpass.getuser()}", "--account-only --report"),
}
WEEKDAYS = {  # --days 縮寫 → (Task Scheduler 星期名, 給人看的名字)
    "mon": ("Monday", t("ext.usage-watch.cli.day_mon")), "tue": ("Tuesday", t("ext.usage-watch.cli.day_tue")),
    "wed": ("Wednesday", t("ext.usage-watch.cli.day_wed")), "thu": ("Thursday", t("ext.usage-watch.cli.day_thu")),
    "fri": ("Friday", t("ext.usage-watch.cli.day_fri")), "sat": ("Saturday", t("ext.usage-watch.cli.day_sat")),
    "sun": ("Sunday", t("ext.usage-watch.cli.day_sun")),
}
MILESTONES = (50, 75, 90)

TOAST_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$t = $x.GetElementsByTagName('text')
$t.Item(0).AppendChild($x.CreateTextNode($env:UW_TITLE)) | Out-Null
$t.Item(1).AppendChild($x.CreateTextNode($env:UW_BODY)) | Out-Null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($x))
"""


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(ln))
        except json.JSONDecodeError:
            print(t("ext.usage-watch.cli.bad_line_skipped", file=path.name, line=ln[:80]), file=sys.stderr)
    return rows


def append_jsonl(path: Path, rows: list[dict]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def at(s: dict) -> datetime:
    return datetime.fromisoformat(s["at"])


def alert(key: str, msg: str, window_h: float) -> dict:
    return {"key": key, "msg": msg, "window_h": window_h}


# ---------- 帳號 ----------

def read_cred() -> dict:
    return json.loads((CLAUDE_DIR / ".credentials.json").read_text(encoding="utf-8"))["claudeAiOauth"]


def refresh_token() -> str | None:
    """token 8 小時過期，桌面版 Code 分頁不會刷新這個檔；`claude auth status` 會順手刷新。失敗回錯誤字串。"""
    import shutil
    exe = shutil.which("claude") or "claude"   # PATH 上的 claude（各平台安裝位置不同），找不到就交給系統報錯
    try:
        r = subprocess.run([exe, "auth", "status"], capture_output=True,
                           text=True, encoding="utf-8", timeout=60, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        return str(e)[:300]
    return None if r.returncode == 0 else (r.stderr.strip()[:300] or f"exit {r.returncode}")


def fetch_account(now: datetime) -> dict:
    cred = read_cred()
    if cred.get("expiresAt", 0) / 1000 < now.timestamp():
        err = refresh_token()
        cred = read_cred()
        if cred.get("expiresAt", 0) / 1000 < now.timestamp():
            detail = t("ext.usage-watch.cli.refresh_error_detail", error=err) if err else ""
            raise RuntimeError(t("ext.usage-watch.cli.token_expired", detail=detail))
    req = urllib.request.Request(USAGE_API, headers={
        "Authorization": "Bearer " + cred["accessToken"],
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "usage-watch",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read().decode("utf-8"))
    week = d.get("seven_day")
    if not week or "utilization" not in week:
        raise RuntimeError(t("ext.usage-watch.cli.no_utilization", response=str(d)[:200]))
    five = d.get("five_hour") or {}
    return {
        "at": now.isoformat(timespec="seconds"),
        "plan": cred.get("rateLimitTier"),
        "weekly": week["utilization"], "weekly_reset": week.get("resets_at"),
        "five_hour": five.get("utilization"), "five_hour_reset": five.get("resets_at"),
        "limits": {k: v["utilization"] for k, v in d.items()
                   if isinstance(v, dict) and v.get("utilization") is not None},
    }


def period_key(s: dict) -> datetime:
    """週期以重置時間（四捨五入到整點）識別；API 每次回的秒數小數會飄。"""
    r = datetime.fromisoformat(s["weekly_reset"]) + timedelta(minutes=30)
    return r.replace(minute=0, second=0, microsecond=0)


def rate_between(prev: dict, cur: dict) -> float | None:
    """每小時漲幾％；跨週期或間隔不在 0.75～3h 回 None。

    API 的％只到整數，間隔太短時差 1％ 就被放大成假突增（23 分鐘 → 2.6%/h 誤差），所以下限 45 分鐘。
    """
    gap = (at(cur) - at(prev)).total_seconds() / 3600
    if period_key(prev) != period_key(cur) or not 0.75 <= gap <= 3:
        return None
    return (cur["weekly"] - prev["weekly"]) / gap


def deltas(samples: list[dict]) -> list[tuple[datetime, float]]:
    """相鄰兩筆之間用掉的％，記在後一筆的時間；跨重置時後一筆的值就是新週期已用量。"""
    out = []
    for p, c in zip(samples, samples[1:]):
        d = c["weekly"] - p["weekly"] if period_key(p) == period_key(c) else c["weekly"]
        out.append((at(c), max(0.0, d)))
    return out


def analyze_account(cur: dict, history: list[dict], now: datetime, a) -> tuple[list[str], list[dict]]:
    samples = history + [cur]
    reset = datetime.fromisoformat(cur["weekly_reset"]).astimezone()
    pk = period_key(cur)
    alerts = []
    lines = [t("ext.usage-watch.cli.weekly_line", weekly=f"{cur['weekly']:.0f}", reset=f"{reset:%m-%d %H:%M}", plan=cur['plan'])]

    # ① 突增
    past_rates = [r for p, c in zip(history, history[1:]) if (r := rate_between(p, c)) is not None and r > 0]
    threshold = max(a.rate_alert, statistics.median(past_rates) * 3) if len(past_rates) >= 10 else a.rate_alert
    # 手動跑會插進間隔很短的筆，往前找最近一筆間隔夠的當比較點
    ref = next((s for s in reversed(history) if rate_between(s, cur) is not None), None)
    rate = rate_between(ref, cur) if ref else None
    if rate is None:
        lines.append(t("ext.usage-watch.cli.rate_no_reference"))
    else:
        flag = rate >= threshold
        lines.append(t("ext.usage-watch.cli.rate_line", rate=f"{rate:+.1f}", threshold=f"{threshold:.1f}") + ("  ⚠" if flag else ""))
        if flag:
            alerts.append(alert("rate", t("ext.usage-watch.cli.alert_rate", rate=f"{rate:.1f}"), 3))

    # ② 會提早用完
    recent = [s for s in samples if period_key(s) == pk and at(s) >= now - timedelta(hours=24)]
    span_h = (now - at(recent[0])).total_seconds() / 3600
    elapsed_h = (now - (reset - timedelta(days=7))).total_seconds() / 3600
    if span_h >= 12:
        pace, basis = (cur["weekly"] - recent[0]["weekly"]) / span_h, t("ext.usage-watch.cli.basis_recent", span_h=f"{span_h:.0f}")
    elif elapsed_h >= 12:
        pace, basis = cur["weekly"] / elapsed_h, t("ext.usage-watch.cli.basis_week_average")
    else:
        pace, basis = None, ""
    if pace is None:
        lines.append(t("ext.usage-watch.cli.pace_too_early"))
    elif pace <= 0:
        lines.append(t("ext.usage-watch.cli.pace_flat", basis=basis))
    else:
        eta = now + timedelta(hours=(100 - cur["weekly"]) / pace)
        if eta < reset:
            lines.append(t("ext.usage-watch.cli.pace_runs_out", basis=basis, eta=f"{eta:%m-%d %H:%M}"))
            alerts.append(alert("pace", t("ext.usage-watch.cli.alert_pace", basis=basis, eta=f"{eta:%m-%d %H:%M}"), 6))
        else:
            left_h = (reset - now).total_seconds() / 3600
            lines.append(t("ext.usage-watch.cli.pace_projection", basis=basis, projected=f"{cur['weekly'] + pace * left_h:.0f}"))

    # ③ 里程碑（每週期只報最高的那一階，各一次）
    hit = [m for m in MILESTONES if cur["weekly"] >= m]
    if hit:
        alerts.append(alert(f"milestone{hit[-1]}@{pk:%Y%m%d%H}", t("ext.usage-watch.cli.alert_milestone", milestone=hit[-1]), 24 * 8))

    # ④ 5 小時窗
    if cur["five_hour"] is not None:
        fh_reset = datetime.fromisoformat(cur["five_hour_reset"]).astimezone()
        flag = cur["five_hour"] >= a.five_hour_alert
        lines.append(t("ext.usage-watch.cli.five_hour_line", five_hour=f"{cur['five_hour']:.0f}", fh_reset=f"{fh_reset:%H:%M}") + ("  ⚠" if flag else ""))
        if flag:
            alerts.append(alert("five_hour", t("ext.usage-watch.cli.alert_five_hour", five_hour=f"{cur['five_hour']:.0f}"), 3))
    else:
        lines.append(t("ext.usage-watch.cli.five_hour_none"))

    # 日
    by_day = defaultdict(float)
    for when, d in deltas(samples):
        by_day[when.astimezone().date()] += d
    today = now.date()
    past = [v for day, v in by_day.items() if today - timedelta(days=28) <= day < today and v > 0]
    day_thr = max(a.daily_budget, statistics.median(past) * 2) if len(past) >= 5 else a.daily_budget
    flag = by_day[today] >= day_thr
    lines.append(t("ext.usage-watch.cli.daily_line", today=f"{by_day[today]:.0f}", day_thr=f"{day_thr:.0f}", yesterday=f"{by_day[today - timedelta(days=1)]:.0f}")
                 + ("  ⚠" if flag else ""))
    if flag:
        alerts.append(alert(f"daily@{today}", t("ext.usage-watch.cli.alert_daily", today=f"{by_day[today]:.0f}"), 24))

    # 週：上週同期＋結算
    target = now - timedelta(days=7)
    cands = [s for s in history if abs(at(s) - target) <= timedelta(hours=2)]
    if cands:
        lw = min(cands, key=lambda s: abs(at(s) - target))["weekly"]
        flag = lw > 0 and cur["weekly"] >= lw * a.week_factor and cur["weekly"] - lw >= 10
        lines.append(t("ext.usage-watch.cli.week_line", lw=f"{lw:.0f}", weekly=f"{cur['weekly']:.0f}") + ("  ⚠" if flag else ""))
        if flag:
            alerts.append(alert("week_vs_last", t("ext.usage-watch.cli.alert_week", weekly=f"{cur['weekly']:.0f}", lw=f"{lw:.0f}"), 24))
    else:
        lines.append(t("ext.usage-watch.cli.week_no_history"))
    last_by_period = {}
    for s in samples:
        last_by_period[period_key(s)] = s["weekly"]
    done = [(k, v) for k, v in sorted(last_by_period.items()) if k != pk][-4:]
    if done:
        lines.append(t("ext.usage-watch.cli.recent_weeks") + "  ".join(t("ext.usage-watch.cli.week_settled", start=f"{k - timedelta(days=7):%m-%d}", pct=f"{v:.0f}") for k, v in done))
    return lines, alerts


def account_check(a, now: datetime) -> tuple[list[str], dict]:
    lines = [t("ext.usage-watch.cli.account_header")]
    try:
        cur = fetch_account(now)
    except (OSError, urllib.error.URLError, KeyError, ValueError, RuntimeError) as e:
        print(t("ext.usage-watch.cli.account_failed_warn", error=e), file=sys.stderr)
        return lines + [t("ext.usage-watch.cli.account_failed_line", error=e)], {
            "ok": False, "error": str(e), "alerts": [alert("error", t("ext.usage-watch.cli.alert_account_failed", error=e), 12)]}
    history = read_jsonl(ACCOUNT_LOG)
    body, alerts = analyze_account(cur, history, now, a)
    append_jsonl(ACCOUNT_LOG, [cur])
    return lines + body, {"ok": True, **cur, "alerts": alerts}


# ---------- 本機 ----------

def merge_ledger(per_hour: dict) -> dict:
    """{hour_dt: {project: [weighted, raw]}} 併進帳本（同格取大），寫回並回傳全部。"""
    ledger = {}
    if LEDGER.exists():
        try:
            ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(t("ext.usage-watch.cli.ledger_corrupt", ledger=LEDGER, error=e), file=sys.stderr)
    for h, projs in per_hour.items():
        slot = ledger.setdefault(h.isoformat(), {})
        for p, (w, r) in projs.items():
            old = slot.get(p, [0, 0])
            slot[p] = [max(old[0], w), max(old[1], r)]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(ledger, sort_keys=True, ensure_ascii=False), encoding="utf-8", newline="\n")
    return {datetime.fromisoformat(k): v for k, v in ledger.items()}


def weighted(u: dict) -> float:
    cc = u.get("cache_creation") or {}
    cc_1h = cc.get("ephemeral_1h_input_tokens", 0) or 0
    cc_5m = (u.get("cache_creation_input_tokens", 0) or 0) - cc_1h
    return ((u.get("input_tokens", 0) or 0)
            + cc_5m * 1.25 + cc_1h * 2
            + (u.get("cache_read_input_tokens", 0) or 0) * 0.1
            + (u.get("output_tokens", 0) or 0) * 5)


def raw_total(u: dict) -> int:
    return sum((u.get(k, 0) or 0) for k in
               ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))


def scan(since: datetime):
    """回傳 [(local_dt, weighted, raw, project, session)]，已去重。"""
    seen: set = set()
    rows = []
    bad_lines = 0
    since_ts = since.timestamp()
    for f in PROJECTS.rglob("*.jsonl"):
        if f.stat().st_mtime < since_ts:
            continue
        project = f.relative_to(PROJECTS).parts[0]
        with f.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"usage"' not in line or '"assistant"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    bad_lines += 1
                    continue
                m = d.get("message") or {}
                u = m.get("usage")
                ts = d.get("timestamp")
                if d.get("type") != "assistant" or not u or not ts:
                    continue
                key = (m.get("id"), d.get("requestId"))
                if key in seen:
                    continue
                seen.add(key)
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
                if dt < since:
                    continue
                rows.append((dt, weighted(u), raw_total(u), project, d.get("sessionId", f.stem)))
    if bad_lines:
        print(t("ext.usage-watch.cli.json_lines_skipped", bad_lines=bad_lines), file=sys.stderr)
    return rows


def percentile(vals: list[float], p: float) -> float:
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p))]


def fmt(n: float) -> str:
    return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.0f}K"


def local_check(a, now: datetime) -> tuple[list[str], dict]:
    this_hour = now.replace(minute=0, second=0, microsecond=0)
    since = this_hour - timedelta(days=a.days)
    rows = scan(since)

    per_hour = defaultdict(dict)
    culprits = defaultdict(Counter)
    for dt, w, r, proj, sess in rows:
        h = dt.replace(minute=0, second=0, microsecond=0)
        slot = per_hour[h].setdefault(proj, [0.0, 0])
        slot[0] += w
        slot[1] += r
        culprits[h][f"{proj} / {sess[:8]}"] += w
    merged = merge_ledger(per_hour)
    earliest = min(merged, default=now)
    merged = {h: v for h, v in merged.items() if h >= since}
    hourly = {h: sum(x[0] for x in v.values()) for h, v in merged.items()}
    hourly_raw = {h: sum(x[1] for x in v.values()) for h, v in merged.items()}

    alerts = []
    recent_start = this_hour - timedelta(hours=a.recent_hours - 1)
    baseline = [v for h, v in hourly.items() if h < recent_start and v > 0]
    lines = [t("ext.usage-watch.cli.hourly_header", days=a.days, hours=len(baseline))]
    if len(baseline) < 10:
        lines.append(t("ext.usage-watch.cli.baseline_too_small"))
    else:
        med = statistics.median(baseline)
        threshold = max(percentile(baseline, 0.95), med * a.hour_factor)
        lines.append(t("ext.usage-watch.cli.median_line", median=fmt(med), threshold=fmt(threshold)))
        recent = [(h, hourly[h]) for h in sorted(hourly) if h >= recent_start]
        peak = max(recent, key=lambda x: x[1], default=None)
        if peak:
            lines.append(t("ext.usage-watch.cli.peak_line", recent_hours=a.recent_hours, hour=f"{peak[0]:%m-%d %H:00}", usage=fmt(peak[1]), ratio=f"{peak[1] / med:.1f}"))
        for h, w in recent:
            if w <= threshold:
                continue
            alerts.append(alert(f"local-hour@{h:%Y%m%d%H}", t("ext.usage-watch.cli.alert_local_hour", hour=f"{h:%m-%d %H:00}", ratio=f"{w / med:.1f}"), 24))
            lines.append(t("ext.usage-watch.cli.abnormal_hour_line", hour=f"{h:%m-%d %H:00}", usage=fmt(w), ratio=f"{w / med:.1f}", raw=fmt(hourly_raw[h])))
            for name, cw in culprits[h].most_common(3):
                lines.append(f"      {fmt(cw):>8}  {name}")
        if not any(x["key"].startswith("local-hour") for x in alerts):
            lines.append(t("ext.usage-watch.cli.no_abnormal_hours"))

    daily = defaultdict(Counter)
    for h, projs in merged.items():
        for p, (w, _) in projs.items():
            daily[h.date()][p] += w
    lines.append(t("ext.usage-watch.cli.daily_header"))
    for i in range(6, -1, -1):
        day = now.date() - timedelta(days=i)
        c = daily.get(day)
        top = t("ext.usage-watch.cli.daily_top", project=c.most_common(1)[0][0], usage=fmt(c.most_common(1)[0][1])) if c else ""
        lines.append(f"  {day:%m-%d}  {fmt(sum(c.values()) if c else 0):>8}{top}")

    weeks = []
    for i in range(a.days // 7):
        end = this_hour + timedelta(hours=1) - timedelta(days=7 * i)
        start = end - timedelta(days=7)
        weeks.append((sum(v for h, v in hourly.items() if start <= h < end), start >= earliest))
    prev = [w for w, full in weeks[1:] if full and w > 0]
    week_ratio = weeks[0][0] / statistics.mean(prev) if prev else None
    lines.append(t("ext.usage-watch.cli.weekly_header", earliest=f"{earliest:%m-%d %H:00}"))
    lines.append("  " + "  ".join(fmt(w) + ("" if full else "*") for w, full in weeks))
    if week_ratio is None:
        lines.append(t("ext.usage-watch.cli.no_full_week"))
    else:
        flag = week_ratio >= a.week_factor
        lines.append(t("ext.usage-watch.cli.week_ratio_line", week_ratio=f"{week_ratio:.2f}") + (t("ext.usage-watch.cli.abnormal_rise") if flag else t("ext.usage-watch.cli.normal")))
        if flag:
            alerts.append(alert("local-week", t("ext.usage-watch.cli.alert_local_week", week_ratio=f"{week_ratio:.1f}"), 24))
    return lines, {"msgs": len(rows), "weeks": [w for w, _ in weeks], "week_ratio": week_ratio, "alerts": alerts}


# ---------- 通知與排程 ----------

def fresh_alerts(alerts: list[dict], log: list[dict], now: datetime) -> list[dict]:
    """去掉冷卻時間內已通知過的同 key 異常。"""
    sent = {}
    for r in log:
        if r.get("notified"):
            sent[r["key"]] = max(sent.get(r["key"], at(r)), at(r))
    return [x for x in alerts if x["key"] not in sent or now - sent[x["key"]] >= timedelta(hours=x["window_h"])]


def toast(title: str, body: str) -> str | None:
    """跳 Windows 通知；失敗回錯誤字串。"""
    r = subprocess.run(["powershell", "-NoProfile", "-Command", TOAST_PS], capture_output=True, text=True,
                       env={**os.environ, "UW_TITLE": title, "UW_BODY": body},
                       creationflags=NO_WINDOW)
    return None if r.returncode == 0 else (r.stderr.strip()[:300] or f"exit {r.returncode}")


def discord_post(url: str, text: str) -> str | None:
    """POST 到 Discord webhook；失敗回錯誤字串（不含網址）。"""
    req = urllib.request.Request(url, data=json.dumps({"content": text[:2000]}).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "User-Agent": "usage-watch"})
    try:
        urllib.request.urlopen(req, timeout=15).close()
    except (OSError, urllib.error.URLError) as e:
        return t("ext.usage-watch.cli.discord_failed", error=e)
    return None


def load_webhook() -> tuple[str | None, str | None]:
    """回傳 (webhook 網址或 None, 錯誤或 None)；沒有 settings.json 不算錯。"""
    if not SETTINGS.exists():
        return None, None
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8")).get("discord_webhook"), None
    except json.JSONDecodeError as e:
        return None, t("ext.usage-watch.cli.settings_unreadable", error=e)


def notify(title: str, body: str) -> tuple[bool, dict]:
    """Windows 通知＋Discord（settings.json 有 webhook 才發）；回傳 (至少一個管道送達, {管道: 錯誤})。"""
    errors = {}
    if err := toast(title, body):
        errors["toast"] = err
    url, err = load_webhook()
    if err:
        errors["discord"] = err
    if url and (err := discord_post(url, f"**{title}**\n{body}")):
        errors["discord"] = err
    sent = "toast" not in errors or bool(url and "discord" not in errors)
    return sent, errors


def report_text(now: datetime, acc_lines: list[str], alerts: list[dict]) -> str:
    """--report 的 Discord 訊息：帳號段原樣放 code block，當下異常列在後面。"""
    body = "\n".join(ln.strip() for ln in acc_lines[1:])
    tail = "".join(f"\n⚠ {x['msg']}" for x in alerts)
    return t("ext.usage-watch.cli.report_message", at=f"{now:%m-%d %H:%M}", body=body, tail=tail)


# ---------- 圖表 ----------

def chart_data(now: datetime, daily_budget: float) -> dict:
    """把 account／alerts／hourly 紀錄整理成圖表要的數列（時間一律毫秒，給 JS 直接用）。"""
    samples = read_jsonl(ACCOUNT_LOG)

    def ms(dt: datetime) -> int:
        return int(dt.timestamp() * 1000)

    marks = []
    for r in read_jsonl(ALERT_LOG):
        prior = [s for s in samples if at(s) <= at(r)]
        if r.get("notified") and prior:
            marks.append([ms(at(r)), prior[-1]["weekly"], r["msg"]])

    by_day = defaultdict(float)
    for when, d in deltas(samples):
        by_day[when.astimezone().date()] += d
    first = min(by_day, default=now.date())
    days = [first + timedelta(days=i) for i in range((now.date() - first).days + 1)][-28:]

    ledger = json.loads(LEDGER.read_text(encoding="utf-8")) if LEDGER.exists() else {}
    per_day = defaultdict(Counter)
    for k, projs in ledger.items():
        for p, (w, _) in projs.items():
            per_day[datetime.fromisoformat(k).date()][p] += w
    pdays = [now.date() - timedelta(days=i) for i in range(13, -1, -1)]
    totals = sum((per_day[d] for d in pdays), Counter())
    top = [p for p, _ in totals.most_common(5)]
    def label(slug: str) -> str:
        """專案資料夾 slug 去掉磁碟代號；太長（桌面 app 暫存工作區帶 UUID）截成頭…尾，免得撐破圖例。"""
        name = re.sub(r"^[A-Za-z]--", "", slug)
        return name if len(name) <= 28 else f"{name[:14]}…{name[-12:]}"

    series = [{"name": label(p), "data": [round(per_day[d][p] / 1e6, 2) for d in pdays]} for p in top]
    other = [round((sum(per_day[d].values()) - sum(per_day[d][p] for p in top)) / 1e6, 2) for d in pdays]
    if any(other):
        series.append({"name": "其他", "data": other})

    return {
        "generated": f"{now:%Y-%m-%d %H:%M}",
        "weekly": [[ms(at(s)), s["weekly"]] for s in samples],
        "resets": sorted({ms(period_key(s)) for s in samples}),
        "alerts": marks,
        "daily": [[f"{d:%m-%d}", round(by_day.get(d, 0), 1)] for d in days],
        "budget": daily_budget,
        "projects": {"days": [f"{d:%m-%d}" for d in pdays], "series": series},
    }


def write_chart(data: dict) -> Path:
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")  # 資料裡的 </script> 不能截斷頁面
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    CHART.write_text(CHART_HTML.replace("__DATA__", payload), encoding="utf-8", newline="\n")
    return CHART


# 顏色取自 dataviz 預設色票（分類 6 槽＋critical），淺／深色各一組；淺色有 3 色對比 <3:1，所以每張圖附數據表。
CHART_HTML = r"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Claude 用量</title>
<style>
:root{color-scheme:light;--surface:#fcfcfb;--text:#0b0b0b;--muted:#52514e;--grid:#e6e5e1;
  --s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--s6:#008300;--critical:#d03b3b}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--surface:#1a1a19;--text:#f0efec;--muted:#c3c2b7;--grid:#383835;
  --s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--critical:#d03b3b}}
body{margin:0;background:var(--surface);color:var(--text);font:14px/1.5 system-ui,"Microsoft JhengHei",sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:0}h2{font-size:16px;margin:0}
.note{color:var(--muted);font-size:13px;margin:2px 0 8px}
section{margin:28px 0}.box{position:relative;height:320px}
details{margin-top:6px;color:var(--muted);font-size:12px}
table{border-collapse:collapse;margin-top:4px}th,td{padding:2px 10px;border-bottom:1px solid var(--grid);text-align:right}
th:first-child,td:first-child{text-align:left}
</style></head><body><main>
<h1>Claude 用量</h1><p class="note" id="gen"></p>
<section><h2>每週額度走勢（帳號，所有機器合計）</h2>
<p class="note">直虛線＝週重置；橫虛線＝100% 上限；▲＝已發通知的異常（滑過看內容）</p>
<div class="box"><canvas id="weekly"></canvas></div>
<details><summary>數據表（最近 48 筆）</summary><table id="t-weekly"></table></details></section>
<section><h2>每日用量（帳號，每週額度的 %）</h2><p class="note" id="daily-note"></p>
<div class="box"><canvas id="daily"></canvas></div>
<details><summary>數據表</summary><table id="t-daily"></table></details></section>
<section><h2>本機各專案每日 token（加權，百萬）</h2>
<p class="note">只算這台電腦的 Claude Code；最近 14 天，前 5 名以外併成「其他」</p>
<div class="box"><canvas id="projects"></canvas></div>
<details><summary>數據表</summary><table id="t-projects"></table></details></section>
</main>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.js"></script>
<script>
const D = __DATA__;
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const C = {text: css('--text'), muted: css('--muted'), grid: css('--grid'), surface: css('--surface'),
           crit: css('--critical'), s: [1, 2, 3, 4, 5, 6].map(i => css('--s' + i))};
const pad = n => String(n).padStart(2, '0');
const fmt = ms => { const d = new Date(ms); return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`; };
Chart.defaults.color = C.muted;
Chart.defaults.borderColor = C.grid;
Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
Chart.defaults.maintainAspectRatio = false;

// 參考線：opts.x＝直線 [毫秒, 標籤]，opts.y＝橫線 [值, 標籤]
const refLines = {id: 'refLines', afterDatasetsDraw(chart, _a, opts) {
  const {ctx, chartArea: a, scales: {x, y}} = chart;
  ctx.save(); ctx.setLineDash([4, 4]); ctx.lineWidth = 1; ctx.strokeStyle = C.muted; ctx.fillStyle = C.muted;
  for (const [v, label] of opts.x || []) {
    const px = x.getPixelForValue(v); if (px < a.left || px > a.right) continue;
    ctx.beginPath(); ctx.moveTo(px, a.top); ctx.lineTo(px, a.bottom); ctx.stroke();
    const w = ctx.measureText(label).width;  // 太靠右就把字放到線的左邊，免得被切掉
    ctx.fillText(label, px + 4 + w > a.right ? px - 4 - w : px + 4, a.top + 12);
  }
  for (const [v, label] of opts.y || []) {
    const py = y.getPixelForValue(v); if (py < a.top || py > a.bottom) continue;
    ctx.beginPath(); ctx.moveTo(a.left, py); ctx.lineTo(a.right, py); ctx.stroke(); ctx.fillText(label, a.left + 4, py - 4);
  }
  ctx.restore();
}};
const table = (id, head, rows) => {
  document.getElementById(id).innerHTML = '<tr>' + head.map(h => `<th>${h}</th>`).join('') + '</tr>' +
    rows.map(r => '<tr>' + r.map(c => `<td>${c}</td>`).join('') + '</tr>').join('');
};
document.getElementById('gen').textContent = `產生時間 ${D.generated}`;

// 每週額度走勢
// X 軸從第一個週期的開始（第一次重置往前 7 天）畫到最近的重置，看得出這週走到哪
const lastT = D.weekly.length ? D.weekly[D.weekly.length - 1][0] : Date.now();
const xMax = Math.max(lastT, ...D.resets.filter(t => t - lastT < 8 * 864e5));
const xMin = D.weekly.length ? Math.min(D.weekly[0][0], ...D.resets.slice(0, 1).map(t => t - 7 * 864e5)) : undefined;
new Chart(document.getElementById('weekly'), {
  type: 'line',
  data: {datasets: [
    {label: '每週額度', data: D.weekly.map(([x, y]) => ({x, y})), borderColor: C.s[0], backgroundColor: C.s[0],
     borderWidth: 2, pointRadius: D.weekly.length <= 60 ? 3 : 0, pointHitRadius: 8, tension: 0},
    {type: 'scatter', label: '已發通知的異常', data: D.alerts.map(([x, y, m]) => ({x, y, m})), pointStyle: 'triangle',
     pointRadius: 6, backgroundColor: C.crit, borderColor: C.surface, borderWidth: 2, hidden: !D.alerts.length},
  ]},
  options: {
    interaction: {mode: 'nearest', axis: 'x', intersect: false},
    scales: {
      x: {type: 'linear', min: xMin, max: xMax,
          ticks: {callback: v => fmt(v).slice(0, 5), maxTicksLimit: 10}, grid: {display: false}},
      y: {min: 0, max: 100, ticks: {callback: v => v + '%'}},
    },
    plugins: {
      legend: {display: D.alerts.length > 0, labels: {usePointStyle: true}},
      tooltip: {callbacks: {title: it => fmt(it[0].parsed.x),
                            label: c => c.raw.m ? `⚠ ${c.raw.m}` : `每週 ${c.parsed.y}%`}},
      refLines: {x: D.resets.map(t => [t, '重置']), y: [[100, '上限 100%']]},
    },
  },
  plugins: [refLines],
});
table('t-weekly', ['時間', '每週 %'], D.weekly.slice(-48).reverse().map(([t, y]) => [fmt(t), y]));

// 每日用量
document.getElementById('daily-note').textContent =
  `橫虛線＝每日預算 ${D.budget}%（約 5 個工作天用完一週）；某天沒有紀錄時為 0`;
new Chart(document.getElementById('daily'), {
  type: 'bar',
  data: {labels: D.daily.map(d => d[0]),
         datasets: [{label: '當日用量', data: D.daily.map(d => d[1]), backgroundColor: C.s[0],
                     borderRadius: {topLeft: 4, topRight: 4}, borderSkipped: 'bottom', maxBarThickness: 28}]},
  options: {
    scales: {x: {grid: {display: false}}, y: {beginAtZero: true, suggestedMax: D.budget * 1.5, ticks: {callback: v => v + '%'}}},
    plugins: {legend: {display: false}, tooltip: {callbacks: {label: c => `當日 ${c.parsed.y}%`}},
              refLines: {y: [[D.budget, `預算 ${D.budget}%`]]}},
  },
  plugins: [refLines],
});
table('t-daily', ['日期', '當日 %'], D.daily.slice().reverse());

// 本機各專案每日 token
const P = D.projects;
new Chart(document.getElementById('projects'), {
  type: 'bar',
  data: {labels: P.days, datasets: P.series.map((s, i) => ({
    label: s.name, data: s.data, backgroundColor: C.s[i], borderColor: C.surface, borderWidth: 1, maxBarThickness: 28}))},
  options: {
    interaction: {mode: 'index', intersect: false},
    scales: {x: {stacked: true, grid: {display: false}}, y: {stacked: true, ticks: {callback: v => v + 'M'}}},
    plugins: {legend: {position: 'bottom'},
              tooltip: {callbacks: {label: c => `${c.dataset.label}：${c.parsed.y}M`,
                                    footer: it => `合計 ${it.reduce((a, c) => a + c.parsed.y, 0).toFixed(2)}M`}}},
  },
});
table('t-projects', ['日期', ...P.series.map(s => s.name)], P.days.map((d, i) => [d, ...P.series.map(s => s.data[i])]).reverse());
</script></body></html>
"""


def _powershell(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                          creationflags=NO_WINDOW)


def report_trigger(at: str, days: str) -> tuple[str, str] | None:
    """report 排程的 (PowerShell 觸發條件, 白話說明)；days（--on）＝daily／weekdays／mon,wed,fri，不認得回 None。"""
    if days == "daily":
        return f"New-ScheduledTaskTrigger -Daily -At {at}", t("ext.usage-watch.cli.daily_at", at=at)
    keys = ["mon", "tue", "wed", "thu", "fri"] if days == "weekdays" else [k.strip() for k in days.lower().split(",")]
    if any(k not in WEEKDAYS for k in keys):
        return None
    return (f"New-ScheduledTaskTrigger -Weekly -DaysOfWeek {','.join(WEEKDAYS[k][0] for k in keys)} -At {at}",
            t("ext.usage-watch.cli.weekly_at", days=t("ext.usage-watch.cli.day_separator").join(WEEKDAYS[k][1] for k in keys), at=at))


def schedule(kind: str, register: bool, at: str, days: str = "daily") -> int:
    """註冊或移除 TASKS[kind]；watch＝每小時（下個整點過 5 分起），report＝days 那幾天的 at。"""
    name, args = TASKS[kind]
    if not register:
        r = _powershell(f"Unregister-ScheduledTask -TaskName '{name}' -Confirm:$false -ErrorAction Stop")
        if r.returncode != 0:
            print(t("ext.usage-watch.cli.unregister_failed", name=name, error=r.stderr.strip()), file=sys.stderr)
            return 1
        print(t("ext.usage-watch.cli.unregistered", name=name))
        return 0
    if kind == "report" and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at):
        print(t("ext.usage-watch.cli.bad_at", at=f"{at!r}"), file=sys.stderr)
        return 1
    if kind == "watch":
        trigger, when = ("New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddHours((Get-Date).Hour + 1).AddMinutes(5) "
                         "-RepetitionInterval (New-TimeSpan -Hours 1)"), t("ext.usage-watch.cli.hourly")
    elif spec := report_trigger(at, days):
        trigger, when = spec
    else:
        print(t("ext.usage-watch.cli.bad_on", days=f"{days!r}"), file=sys.stderr)
        return 1
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    r = _powershell(f"""
$a = New-ScheduledTaskAction -Execute '{pythonw}' -Argument '"{Path(__file__).resolve()}" {args}'
$t = {trigger}
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName '{name}' -Action $a -Trigger $t -Settings $s -Force | Out-Null
(Get-ScheduledTaskInfo -TaskName '{name}').NextRunTime
""")
    if r.returncode != 0:
        print(t("ext.usage-watch.cli.register_failed", name=name, error=r.stderr.strip()), file=sys.stderr)
        return 1
    print(t("ext.usage-watch.cli.registered", name=name, when=when, next_run=r.stdout.strip()))
    return 0


def status() -> int:
    """各排程的上次／下次執行與結果，加上 last-run.json 摘要；watch 沒註冊才算失敗（report 是選用）。"""
    watch_missing = False
    for kind, (name, _) in TASKS.items():
        # 267011 = SCHED_S_TASK_HAS_NOT_RUN（註冊後還沒跑過，LastRunTime 會是 1999 年）
        r = _powershell(f"$i = Get-ScheduledTaskInfo -TaskName '{name}' -ErrorAction Stop; "
                        "'{0:MM-dd HH:mm}|{1}|{2:MM-dd HH:mm}' -f $i.LastRunTime, $i.LastTaskResult, $i.NextRunTime")
        fields = r.stdout.strip().split("|")   # 上次執行｜結果碼｜下次執行：PowerShell 只回值，字在這裡組（走語言表）
        if r.returncode != 0:
            line = t("ext.usage-watch.cli.status_not_registered", kind=kind)
        elif len(fields) != 3:
            line = r.stdout.strip()            # 不是預期的三欄：原樣印，看得到 PowerShell 回了什麼
        else:
            last_run, result, next_run = fields
            last = (t("ext.usage-watch.cli.status_not_run") if result == "267011"
                    else t("ext.usage-watch.cli.status_last_result", at=last_run, result=result))
            line = t("ext.usage-watch.cli.status_schedule", last=last, next=next_run).rstrip()   # 沒有下次執行時間就不留尾巴空白
        print(t("ext.usage-watch.cli.status_task", name=name, line=line))
        watch_missing |= kind == "watch" and r.returncode != 0
    if LAST_RUN.exists():
        d = json.loads(LAST_RUN.read_text(encoding="utf-8"))
        problems = ([d.get("error")] if not d.get("ok") else []) + list((d.get("notify_errors") or {}).values())
        summary = t("ext.usage-watch.cli.problem_separator").join(problems) if problems else t("ext.usage-watch.cli.status_ok")
        print(t("ext.usage-watch.cli.status_last_run", at=d.get('at'), summary=summary, alerts=len(d.get('alerts') or [])))
    else:
        print(t("ext.usage-watch.cli.no_runs_yet"))
    return int(watch_missing)


def main() -> int:
    ap = argparse.ArgumentParser(description=t("ext.usage-watch.cli.description"))
    ap.add_argument("--account-only", action="store_true", help=t("ext.usage-watch.cli.account_only_help"))
    ap.add_argument("--notify", action="store_true", help=t("ext.usage-watch.cli.notify_help"))
    ap.add_argument("--test-notify", action="store_true", help=t("ext.usage-watch.cli.test_notify_help"))
    ap.add_argument("--report", action="store_true", help=t("ext.usage-watch.cli.report_help"))
    ap.add_argument("--chart", action="store_true", help=t("ext.usage-watch.cli.chart_help"))
    ap.add_argument("--status", action="store_true", help=t("ext.usage-watch.cli.status_help"))
    ap.add_argument("--register", nargs="?", const="watch", choices=list(TASKS),
                    help=t("ext.usage-watch.cli.register_help"))
    ap.add_argument("--unregister", nargs="?", const="watch", choices=list(TASKS), help=t("ext.usage-watch.cli.unregister_help"))
    ap.add_argument("--at", default="09:00", help=t("ext.usage-watch.cli.at_help"))
    ap.add_argument("--on", default="daily",
                    help=t("ext.usage-watch.cli.on_help"))
    ap.add_argument("--rate-alert", type=float, default=4.0, help=t("ext.usage-watch.cli.rate_alert_help"))
    ap.add_argument("--five-hour-alert", type=float, default=80.0, help=t("ext.usage-watch.cli.five_hour_alert_help"))
    ap.add_argument("--daily-budget", type=float, default=20.0, help=t("ext.usage-watch.cli.daily_budget_help"))
    ap.add_argument("--week-factor", type=float, default=1.5, help=t("ext.usage-watch.cli.week_factor_help"))
    ap.add_argument("--days", type=int, default=28, help=t("ext.usage-watch.cli.days_help"))
    ap.add_argument("--recent-hours", type=int, default=24, help=t("ext.usage-watch.cli.recent_hours_help"))
    ap.add_argument("--hour-factor", type=float, default=3.0, help=t("ext.usage-watch.cli.hour_factor_help"))
    ap.add_argument("--json", action="store_true", help=t("ext.usage-watch.cli.json_help"))
    a = ap.parse_args()
    if a.register or a.unregister:
        return schedule(a.register or a.unregister, bool(a.register), a.at, a.on)
    if a.status:
        return status()
    if a.chart:
        now = datetime.now().astimezone()
        if not a.account_only:
            local_check(a, now)  # 只為了把本機帳本更新到最新，輸出不用
        path = write_chart(chart_data(now, a.daily_budget))
        print(t("ext.usage-watch.cli.chart_created", path=path))
        if hasattr(os, "startfile"):
            try:
                os.startfile(path)
            except OSError as e:
                print(t("ext.usage-watch.cli.browser_failed", error=e), file=sys.stderr)
        return 0
    if a.test_notify:
        sent, errors = notify(t("ext.usage-watch.cli.test_title"), t("ext.usage-watch.cli.test_body"))
        discord_state = errors.get("discord") or ("OK" if SETTINGS.exists() else t("ext.usage-watch.cli.not_configured"))
        print(t("ext.usage-watch.cli.test_result", toast=errors.get('toast') or 'OK', discord_state=discord_state))
        return 0 if sent and not errors else 1

    now = datetime.now().astimezone()
    acc_lines, acc = account_check(a, now)
    loc_lines, loc = ([], None) if a.account_only else local_check(a, now)
    alerts = acc["alerts"] + (loc["alerts"] if loc else [])

    new = fresh_alerts(alerts, read_jsonl(ALERT_LOG), now)
    notified, notify_errors = False, {}
    if a.notify and new:
        body = "\n".join(f"• {x['msg']}" for x in new)
        if acc["ok"]:
            reset = datetime.fromisoformat(acc["weekly_reset"]).astimezone()
            body += t("ext.usage-watch.cli.alert_body_tail", weekly=f"{acc['weekly']:.0f}", reset=f"{reset:%m-%d %H:%M}")
        notified, notify_errors = notify(t("ext.usage-watch.cli.alert_title"), body)
        for channel, err in notify_errors.items():
            print(t("ext.usage-watch.cli.notify_failed_warn", channel=channel, error=err), file=sys.stderr)
    if a.report:
        url, err = load_webhook()
        err = err or (discord_post(url, report_text(now, acc_lines, alerts)) if url else t("ext.usage-watch.cli.no_webhook"))
        if err:
            notify_errors["discord"] = err
            print(t("ext.usage-watch.cli.report_not_sent", error=err), file=sys.stderr)
        else:
            print(t("ext.usage-watch.cli.report_sent"))
    stamp = now.isoformat(timespec="seconds")
    append_jsonl(ALERT_LOG, [{"at": stamp, "key": x["key"], "msg": x["msg"], "notified": notified and x in new}
                             for x in alerts])
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LAST_RUN.write_text(json.dumps({
        "at": stamp, "ok": acc["ok"], "error": acc.get("error"), "alerts": [x["msg"] for x in alerts],
        "notified": [x["msg"] for x in new] if notified else [], "notify_errors": notify_errors,
        "scheduled": a.notify,  # 排程的 watch 跑法帶 --notify；手動查一次不算排程，SessionStart 不會因它逾期而提醒
    }, ensure_ascii=False), encoding="utf-8", newline="\n")

    if a.json:
        print(json.dumps({"now": stamp, "account": acc, "local": loc}, ensure_ascii=False, default=str))
    else:
        print("\n".join(acc_lines + loc_lines))
        if alerts:
            print(t("ext.usage-watch.cli.alerts_header"))
            for x in alerts:
                print(f"  ⚠ {x['msg']}" + ("" if x in new else t("ext.usage-watch.cli.cooling_down")))

    print(f"USAGE_WATCH DONE account_ok={int(acc['ok'])} alerts={len(alerts)} new={len(new)} "
          f"notified={int(bool(notified and new))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
