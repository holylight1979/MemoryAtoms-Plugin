---
name: usage-watch
description: "Check the current Claude account usage (weekly %, 5-hour window, burn rate, projected exhaustion, daily usage - summed over all machines and the web) and anomalies, send a usage summary to Discord, test usage notifications, and view or register the monitoring schedules. Use when the user says 「Claude 用量多少」「額度還剩多少」「這週會不會爆」「發用量到 Discord」「用量監控有沒有在跑」「測一下用量通知」「/memoryatoms-usage-watch:usage-watch」. For one session's tokens use /token, not this."
user-invocable: true
when_to_use: 觸發詞：Claude 用量, 額度還剩多少, 每週額度, 這週會不會爆, 用量摘要發 Discord, 用量監控狀態, 用量通知測試, 用量排程, usage-watch
metadata:
  pattern: tool-wrapper
---

# Skill: usage-watch (Tool Wrapper)

> Wraps `${CLAUDE_PLUGIN_ROOT}/tools/usage-watch.py`: asks Anthropic for the account quota with Claude Code's login token, judges anomalies, keeps records, and sends notifications.

## When to use

- Asked about account usage, remaining quota, or whether this week's pace is enough.
- To send current usage to Discord, view the chart, test that notifications work, check the schedules are running, or rebuild them.
- To find out which project is burning quota: add the local section (counts only this computer's transcripts).
- Not for: tokens used by a single session (/token), API prices, billing amounts.

## How to call

| What to do | Command |
|----------|------|
| Account usage (about 1 s) | `python ${CLAUDE_PLUGIN_ROOT}/tools/usage-watch.py --account-only` |
| Account plus local per-project (about 10 s) | `python ${CLAUDE_PLUGIN_ROOT}/tools/usage-watch.py` |
| Send a summary to Discord | `python ${CLAUDE_PLUGIN_ROOT}/tools/usage-watch.py --account-only --report` |
| Chart (weekly trend, daily usage, per-project tokens) | `python ${CLAUDE_PLUGIN_ROOT}/tools/usage-watch.py --chart` (writes `workflow/usage-watch/chart.html` and opens it; add `--account-only` to skip scanning local transcripts) |
| Test notifications (Windows + Discord) | `python ${CLAUDE_PLUGIN_ROOT}/tools/usage-watch.py --test-notify` |
| Schedules and latest result | `python ${CLAUDE_PLUGIN_ROOT}/tools/usage-watch.py --status` |
| Create/rebuild hourly monitor (notifies only on anomaly) | `python ${CLAUDE_PLUGIN_ROOT}/tools/usage-watch.py --register` |
| Create/change scheduled summary (daily, weekdays, or chosen days) | `python ${CLAUDE_PLUGIN_ROOT}/tools/usage-watch.py --register report --at HH:MM [--on weekdays\|mon,wed,fri]` (without `--on` = daily) |
| Remove a schedule | `python ${CLAUDE_PLUGIN_ROOT}/tools/usage-watch.py --unregister [watch\|report]` (default = watch) |

- Threshold parameters (`--rate-alert`, `--daily-budget`, `--five-hour-alert`, `--week-factor`) and full help: see `--help`.
- A normal run ends with `USAGE_WATCH DONE account_ok= alerts= new= notified=`; `account_ok=0` means the account query failed, with the reason on the `✖` line.
- Creating schedules and sending to Discord are outward actions: do them only when the user asked this time.

## Reading the result

- **Percent only, no token count**: Anthropic returns only the account quota percentage (integer); the local section's "weighted tokens" counts only this computer.
- **(1) Spike**: hourly rise versus the reading 45 minutes to 3 hours ago; for short gaps the integer % is exaggerated, so nothing closer is used.
- **(2) Projection**: usage at reset projected from the last 24 hours' speed (falls back to the weekly average under 12 hours); running out before reset is an anomaly.
- **Daily / weekly**: rely on the records the schedule accumulates hourly; right after creating the schedule, "same time last week" and "recent weeks' totals" are empty.
- **Query failure "token expired"**: on expiry the tool first runs `claude auth status` so Claude Code refreshes itself, then queries again (the desktop Code tab does not refresh `.credentials.json`); if it still fails, it reports the error and the user should run `claude auth status` or `/login` in a terminal. The tool never calls OAuth refresh itself (it rotates the refresh token and breaks the login).
- **Record location**: `~/.claude/workflow/usage-watch/` (account/alerts jsonl, hourly.json, last-run.json; settings.json holds the Discord webhook and is not version-controlled).

## Anti-patterns

- Answering "how much did the account use" with local transcript token counts (this computer only)
- Calling the API with the token or computing quota by hand, bypassing the tool
- Running `--register` or `--report` on the side when the user only asked about usage
- Pasting the webhook URL or settings.json content into a reply, commit, or document
