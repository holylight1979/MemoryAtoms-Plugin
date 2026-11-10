#!/usr/bin/env python3
"""usage_snapshot.py — 每週用量刷新前把 Claude Code 用量截圖存檔（Windows 工作排程器驅動）。

做什麼：
  先截 VS Code 裡 Claude Code 的 Account & Usage 面板（帳號、方案、每週與 Fable 上限、哪些東西吃掉用量、子代理與 MCP 佔比）：
  另開一個專屬 VS Code（vscode-data/，跟日常那個分開才能帶偵錯埠讓 Playwright 接上；登入沿用這台 Claude Code），
  Claude Code 分頁輸入 /usage、面板切 Week、只截面板。帳號＝面板上的 Email @ 前那段。
  那條失敗（沒裝 VS Code、擴充套件改版、逾時）才改截 claude.ai 用量頁，原因記進 notes：
  用本工具專屬的 Chrome profile（browser-data/，只登入 claude.ai；日常 Chrome 的 profile 被 Chrome 鎖住、
  且 Chrome 136+ 拒絕在預設 profile 上被自動化，所以不能直接借用）
  開有頭 Chrome 視窗（headless 會被 Cloudflare 人類驗證擋下）到 https://claude.ai/settings/usage，點開收合的「本週」列後截全頁；
  帳號＝截圖裡登入 claude.ai 的那個信箱 @ 前那段（頁面 /api/account），跟這台 Claude Code 登入帳號
  （.claude.json，跟 Claude Code 設定目錄）不同時照截圖命名並記警告，讀不到才退用 Claude Code 帳號。
  兩種截圖都存本機 workflow/usage-snapshots/usage-YYYYMMDD-<帳號>.png，不外送。
  設定 usage_watch.snapshot_push_to_org=true（出廠 false，寫在 workflow/config.local.json）才改存組織記憶庫
  （org_memory 那個 repo）的 usage-snapshots/ 並隨即 commit／push（走記憶庫同一套背景上版控 wg_vcs_sync）——
  整個組織都看得到這個帳號的用量。開了但這台沒接上組織記憶庫（先跑 tools/org-memory.py --join）或推不上去
  → 截圖留本機／留在 clone 內，結果標 repo_error。
  面板／頁面文字追記到本機 workflow/usage-snapshots/usage-log.jsonl（source 標 vscode 或 web），
  usage-last-run.json 記最近一次結果（成功／失敗原因）。
  失敗也會在本機留診斷圖：VS Code 那條 usage-YYYYMMDD-vscode-FAILED.png、網頁那條 usage-YYYYMMDD-<帳號>-FAILED.png，不靜默。
  排程跟著帳號的週重置走（每個帳號重置時刻不同，例：週一 21:00、週二 03:40）：截網頁時順手讀用量頁自己打的 /usage API，
  週額度有多條（全部模型一條、單一模型各一條）取用量最高那條，把排程的每週觸發對齊到它重置前 LEAD_MIN 分鐘；
  結果寫在 last-run 的 weekly_used／weekly_reset／schedule。VS Code 面板那條沒有重置時間來源，排程不動（schedule 註明）。

怎麼跑：
  python tools/usage-snapshot/usage_snapshot.py --login     網頁退路首次：開有頭視窗，手動登入 claude.ai，登入成功自動關閉
  python tools/usage-snapshot/usage_snapshot.py             截一張（會彈 VS Code 視窗約 20 秒；失敗再彈 Chrome 約 30 秒）
  python tools/usage-snapshot/usage_snapshot.py --headless  網頁退路不彈視窗（實測會被 Cloudflare 擋，留作再試）
  python tools/usage-snapshot/usage_snapshot.py --register  註冊排程：每週二 03:30 起跳，之後截網頁時自動對齊重置時間（喚醒電腦、錯過補跑）
  完整參數：--help
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# pythonw（Task Scheduler 靜默跑）下 sys.stdout/stderr 為 None → 導到 devnull。
for _name in ("stdout", "stderr"):
    _s = getattr(sys, _name)
    if _s is None:
        setattr(sys, _name, open(os.devnull, "w", encoding="utf-8", newline="\n"))
    else:
        _s.reconfigure(encoding="utf-8", errors="replace")

TOOL_DIR = Path(__file__).resolve().parent
_CORE_DIR = Path(os.environ.get("WG_CORE_ROOT") or TOOL_DIR.parents[3])   # 核心程式碼根：插件版由啟動器給 WG_CORE_ROOT，安裝版是本檔往上
sys.path.insert(0, str(_CORE_DIR / "hooks"))
from wg_core import CLAUDE_DIR, load_config, org_memory_root  # noqa: E402
from wg_vcs_sync import collect_sync_targets, sync_targets_inline  # noqa: E402
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))
from lib.project_root import claude_global_config  # noqa: E402
from lib.i18n import t  # noqa: E402

# 資料都在資料根（WG_CLAUDE_DIR 或 ~/.claude）；一般安裝兩根同一路徑，登入 profile 仍在原處
PROFILE_DIR = CLAUDE_DIR / "tools" / "usage-snapshot" / "browser-data"   # Chrome 登入 profile（使用者資料）
OUT_DIR = CLAUDE_DIR / "workflow" / "usage-snapshots"  # 本機：截圖 / log / last-run / FAILED 圖
REPO_SUBDIR = "usage-snapshots"  # 組織記憶庫內的截圖目錄（wg_vcs_sync org_extra_pathspecs 同名）
LAST_RUN = OUT_DIR / "usage-last-run.json"
LOG = OUT_DIR / "usage-log.jsonl"
USAGE_URL = "https://claude.ai/settings/usage"
TASK_NAME = "Claude-Usage-WeeklySnapshot"
USAGE_TEXT = re.compile(r"\d+%\s*(used|已使用)|(used|已使用)\s*\d+%", re.I)  # 英文「68% used」、中文「已使用 68%」兩種字序
WEEKLY_TOGGLE = re.compile(r"本週|this week|weekly", re.I)  # 用量彈窗「本週」收合鈕的文字
LEAD_MIN = 10  # 週重置前幾分鐘截
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# VS Code 用量面板：另開一個專屬 VS Code（跟日常那個分開，才能帶偵錯埠讓 Playwright 接上）
CODE_EXE = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Microsoft VS Code" / "Code.exe"
VSCODE_DATA = PROFILE_DIR.parent / "vscode-data"   # 專屬 VS Code 設定資料夾（使用者資料，跟 Chrome profile 同處）
VSCODE_WS = PROFILE_DIR.parent / "vscode-workspace"   # 專屬 VS Code 開的空資料夾（Claude Code 分頁要有工作區）
# 視窗被遮住、螢幕鎖定時 Chromium 照常繪製，不然點擊與截圖會卡在等畫面
NO_OCCLUSION_ARGS = ["--disable-features=CalculateNativeWinOcclusion", "--disable-backgrounding-occluded-windows",
                     "--disable-renderer-backgrounding", "--disable-background-timer-throttling"]
VSCODE_ATTEMPTS = 3   # VS Code 那條連試幾次都失敗才退網頁
CC_INPUT = '[role=textbox][aria-label="Message input"]'   # Claude Code 分頁的輸入框
PANEL_LOADED_JS = """s => { const d = [...document.querySelectorAll('[role=dialog]')]
  .find(x => x.innerText.includes('Account & Usage')); return d && !d.innerText.includes(s) }"""
PANEL_HAS_JS = "s => [...document.querySelectorAll('[role=dialog]')].some(x => x.innerText.includes(s))"


def log(msg: str) -> None:
    print(f"[usage-snapshot] {msg}", flush=True)


def account_of(email: str) -> str:
    """信箱 @ 前那段，換掉檔名不能用的字；空信箱回空字串。"""
    return re.sub(r"[^\w.-]", "_", email.split("@")[0])


def detect_account() -> str:
    """這台 Claude Code 登入信箱 @ 前那段；讀不到回空字串。"""
    try:
        data = json.loads(claude_global_config().read_text(encoding="utf-8"))
        email = (data.get("oauthAccount") or {}).get("emailAddress") or ""
    except (OSError, json.JSONDecodeError, AttributeError):
        return ""
    return account_of(email)


def resolve_account(page) -> tuple[str, str | None]:
    """檔名用的帳號＝截圖裡登入 claude.ai 的那個（工具專屬 profile 可能跟這台 Claude Code 登入不同帳號），
    讀不到退用這台 Claude Code 帳號。兩者不同或退用時附一句說明，記進結果不靜默。"""
    cc = detect_account()
    try:
        data = page.evaluate("fetch('/api/account').then(r => r.ok ? r.json() : {})")
        shown = account_of((data or {}).get("email_address") or "")
    except Exception:
        shown = ""
    if not shown:
        return cc, t("ext.usage-watch.snapshot.account_unreadable")
    if cc and shown != cc:
        return shown, t("ext.usage-watch.snapshot.account_mismatch", shown=shown, cc=cc)
    return shown, None


def launch(headed: bool):
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError:
        raise SystemExit(
            t("ext.usage-watch.snapshot.playwright_missing_exit", python=sys.executable))

    p = sync_playwright().start()
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        channel="chrome",
        headless=not headed,
        viewport={"width": 1280, "height": 1000},
        ignore_default_args=["--enable-automation"],
        args=["--disable-blink-features=AutomationControlled"],
    )
    return p, ctx


def expand_weekly(page) -> str | None:
    """用量彈窗裡「本週」那列預設收合，Fable 等分模型的每週上限藏在裡面：收合就點開。
    沒有這顆鈕（帳號沒有分模型上限）照原樣截；點不開回一句說明，不影響主截圖。"""
    toggle = page.locator("[role=dialog] button[aria-expanded=false]").filter(has_text=WEEKLY_TOGGLE)
    try:
        if toggle.count():
            toggle.first.click(timeout=5000)
    except Exception as e:
        return t("ext.usage-watch.snapshot.weekly_toggle_failed", error=type(e).__name__)
    return None


def usage_visible(page) -> bool:
    try:
        return USAGE_TEXT.search(page.locator("body").inner_text(timeout=3000)) is not None
    except Exception:
        return False


def do_login(timeout_min: int) -> int:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    p, ctx = launch(headed=True)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(USAGE_URL, wait_until="domcontentloaded")
    log(t("ext.usage-watch.snapshot.login_prompt", timeout_min=timeout_min))
    deadline = datetime.now().timestamp() + timeout_min * 60
    ok = False
    try:
        while datetime.now().timestamp() < deadline:
            if page.is_closed():
                break
            if usage_visible(page):
                ok = True
                break
            page.wait_for_timeout(2000)
    finally:
        try:
            ctx.close()
        finally:
            p.stop()
    log(t("ext.usage-watch.snapshot.login_done") if ok else t("ext.usage-watch.snapshot.login_not_detected"))
    return 0 if ok else 1


def do_snapshot(headed: bool) -> int:
    """先截 VS Code 裡 Claude Code 的 Account & Usage 面板（資訊多）；那條失敗才改截 claude.ai 用量頁，原因記進 notes。"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    result = {"at": now.isoformat(timespec="seconds"), "ok": False, "file": None, "error": None}
    errors = []
    for _ in range(VSCODE_ATTEMPTS):
        error = capture_vscode(result, now)
        if error is None:
            break
        errors.append(error)
    if errors:  # 重試後才成功也記，時序不穩要看得到
        result["notes"] = [t("ext.usage-watch.snapshot.vscode_failed", count=len(errors), errors=t("ext.usage-watch.snapshot.error_separator").join(errors))]
    if not result["ok"]:
        result["notes"].append(t("ext.usage-watch.snapshot.fallback_web"))
        capture_web(result, now, headed)
    if result["ok"]:
        reset = result.get("weekly_reset")
        result["schedule"] = (align_schedule(datetime.fromisoformat(reset)) if reset
                              else t("ext.usage-watch.snapshot.no_reset_time"))
    if result["ok"] and push_to_org_enabled() and not result.get("repo_error"):
        _push_to_repo(result)
    _finish(result)
    return 0 if result["ok"] else 1


def poll(fn, timeout_s: float, what: str, page=None):
    """每秒試一次 fn，拿到真值就回傳；逾時丟 TimeoutError（訊息寫在等什麼）。
    給 page 就用它等：同步版 Playwright 只在呼叫它時收事件，用 time.sleep 等，新開的框架永遠不會出現在 frames 裡。"""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            got = fn()
        except Exception:
            got = None
        if got:
            return got
        if time.monotonic() > deadline:
            raise TimeoutError(t("ext.usage-watch.snapshot.poll_timeout", timeout_s=f"{timeout_s:.0f}", what=what))
        if page is None:
            time.sleep(1)
        else:
            page.wait_for_timeout(1000)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def capture_vscode(result: dict, now: datetime) -> str | None:
    """開專屬 VS Code（獨立設定資料夾＋偵錯埠，不碰日常那個）→ Claude Code 分頁輸入 /usage → 面板切 Week → 只截面板。
    成功把檔案、帳號、面板文字寫進 result 回 None；失敗回原因（呼叫端改截網頁）。"""
    if not CODE_EXE.is_file():
        return t("ext.usage-watch.snapshot.vscode_not_found", path=CODE_EXE)
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError:
        return t("ext.usage-watch.snapshot.playwright_missing", python=sys.executable)
    VSCODE_WS.mkdir(parents=True, exist_ok=True)
    port = free_port()
    # 從 VS Code 終端機手動跑時帶著這些變數：Code.exe 會被當成 Node 執行、或把視窗轉給日常那個 VS Code
    env = {k: v for k, v in os.environ.items() if k != "ELECTRON_RUN_AS_NODE" and not k.startswith("VSCODE_")}
    proc = subprocess.Popen([str(CODE_EXE), f"--user-data-dir={VSCODE_DATA}", f"--remote-debugging-port={port}",
                             "--new-window", "--disable-workspace-trust", "--skip-release-notes",
                             "--skip-welcome",  # 新設定資料夾第一次開會跳「登入 Copilot」引導視窗擋住整個畫面
                             *NO_OCCLUSION_ARGS,
                             str(VSCODE_WS)], env=env)
    try:
        with sync_playwright() as p:
            _shoot_usage_panel(p, port, result, now)
        return None
    except Exception as e:
        return f"{type(e).__name__}: {(str(e).splitlines() or [''])[0][:200]}"
    finally:  # /T 連 Claude Code 擴充套件起的 CLI 一起收
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _shoot_usage_panel(p, port: int, result: dict, now: datetime) -> None:
    browser = poll(lambda: p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=3000), 60, t("ext.usage-watch.snapshot.wait_debug_port"))
    wb = poll(lambda: next((pg for c in browser.contexts for pg in c.pages if "workbench" in pg.url), None),
              30, t("ext.usage-watch.snapshot.wait_main_window"))
    try:
        wb.wait_for_selector(".monaco-workbench", timeout=60000)
        wb.set_viewport_size({"width": 1280, "height": 2000})  # 撐高到面板不必捲動，不受螢幕大小限制
        wb.wait_for_timeout(5000)  # 等擴充套件啟動（onStartupFinished）
        wb.keyboard.press("Control+Shift+P")
        wb.wait_for_timeout(800)
        wb.keyboard.type("Claude Code: Open in New Tab")
        wb.wait_for_timeout(800)
        wb.keyboard.press("Enter")
        cc = poll(lambda: next((f for f in wb.frames
                                if "claude-code" in f.url.lower() and f.locator(CC_INPUT).count()), None),
                  60, t("ext.usage-watch.snapshot.wait_claude_tab"), page=wb)
        cc.locator(CC_INPUT).focus()  # 不用 click：視窗被遮住時 click 會卡在等畫面穩定
        wb.keyboard.type("/usage")
        wb.wait_for_timeout(1000)
        wb.keyboard.press("Enter")
        panel = cc.locator("[role=dialog]").filter(has_text="Account & Usage")
        panel.wait_for(timeout=30000)
        cc.wait_for_function(PANEL_LOADED_JS, arg="Loading usage data", timeout=90000)
        week = panel.get_by_text("Week", exact=True)
        if week.count():  # 「哪些東西吃掉用量」預設看 Day，每週存檔切 Week
            week.dispatch_event("click")
            cc.wait_for_function(PANEL_HAS_JS, arg="Last 7d", timeout=15000)
        cc.wait_for_timeout(1500)
        text = panel.inner_text()
        email = re.search(r"Email\s+(\S+@\S+)", text)
        account = (account_of(email.group(1)) if email else detect_account()) or "unknown"
        out = _pick_save_dir(result) / f"usage-{now:%Y%m%d}-{account}.png"
        panel.screenshot(path=str(out))
    except Exception:
        try:
            wb.screenshot(path=str(OUT_DIR / f"usage-{now:%Y%m%d}-vscode-FAILED.png"))
        except Exception:
            pass
        raise
    result.update(ok=True, file=str(out), source="vscode",
                  lines=[ln.strip() for ln in text.splitlines() if ln.strip()])


def capture_web(result: dict, now: datetime, headed: bool) -> None:  # headed=False 實測被 Cloudflare 擋
    """claude.ai 用量頁（專屬 Chrome profile）；結果寫進 result。"""
    if not PROFILE_DIR.exists():
        result["error"] = t("ext.usage-watch.snapshot.profile_missing")
        return

    p, ctx = launch(headed)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    usage_api: dict = {}
    page.on("response", lambda resp: _grab_usage_api(resp, usage_api))
    try:
        page.goto(USAGE_URL, wait_until="domcontentloaded", timeout=60000)
        try:
            page.locator("body").filter(has_text=USAGE_TEXT).wait_for(timeout=45000)
        except Exception:
            url = page.url
            if "/login" in url or "/magic-link" in url:
                result["error"] = t("ext.usage-watch.snapshot.not_logged_in", url=url)
            else:
                result["error"] = t("ext.usage-watch.snapshot.usage_text_timeout", url=url, title=f"{page.title()!r}")
        expand_note = expand_weekly(page) if result["error"] is None else None
        page.wait_for_timeout(2500)  # 用量條動畫跑完
        account, account_note = resolve_account(page)
        result.setdefault("notes", []).extend(n for n in (expand_note, account_note) if n)
        if not account and result["error"] is None:
            result["error"] = t("ext.usage-watch.snapshot.account_unknown")
        stamp = now.strftime("%Y%m%d") + "-" + (account or "unknown")
        if result["error"] is None:
            out = _pick_save_dir(result) / f"usage-{stamp}.png"
        else:
            out = OUT_DIR / f"usage-{stamp}-FAILED.png"
        page.screenshot(path=str(out), full_page=True)
        result["file"] = str(out)
        if result["error"] is None:
            result["ok"] = True
            result["source"] = "web"
            body = page.locator("body").inner_text()
            lines = [ln.strip() for ln in body.splitlines()
                     if "%" in ln or "Resets" in ln or "重設" in ln]
            result["lines"] = lines
            top = _top_weekly_limit(usage_api)
            if top:
                name = ((top.get("scope") or {}).get("model") or {}).get("display_name") or t("ext.usage-watch.snapshot.all_models")
                result["weekly_percent"] = top["percent"]
                result["weekly_used"] = t("ext.usage-watch.snapshot.weekly_used", percent=top['percent'], name=name)
                result["weekly_reset"] = _reset_time(top).isoformat(timespec="minutes")
    except Exception as e:  # 瀏覽器層失敗（啟動、導航）
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        try:
            ctx.close()
        finally:
            p.stop()


def _grab_usage_api(resp, into: dict) -> None:
    """用量頁自己會打 /api/organizations/<org>/usage，回應裡有各額度的 resets_at。"""
    if not resp.url.split("?")[0].endswith("/usage"):
        return
    try:
        into.update(resp.json())
    except Exception:
        pass  # 沒抓到 → _top_weekly_limit 回 None，result["schedule"] 會標示


def _top_weekly_limit(usage_api: dict) -> dict | None:
    """週額度有多條（全部模型一條、單一模型各一條）；用量最高的那條當本週用量與重置時間的依據。"""
    weekly = [lim for lim in usage_api.get("limits") or []
              if lim.get("group") == "weekly" and lim.get("resets_at") and lim.get("percent") is not None]
    return max(weekly, key=lambda lim: lim["percent"]) if weekly else None


def _reset_time(limit: dict) -> datetime:
    """額度的下次重置時間（本機時區、取整到分；API 值在整點前後抖幾百毫秒）。"""
    dt = datetime.fromisoformat(limit["resets_at"]).astimezone() + timedelta(seconds=30)
    return dt.replace(second=0, microsecond=0)


def _powershell(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def align_schedule(reset: datetime) -> str:
    """把排程的每週觸發對齊到「週重置前 LEAD_MIN 分鐘」，回一句結果；排程沒註冊就不動。"""
    target = reset - timedelta(minutes=LEAD_MIN)
    day, at = DAYS[target.weekday()], target.strftime("%H:%M")
    r = _powershell(f"""
$task = Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue
if (-not $task) {{ 'none'; exit }}
$cur = $task.Triggers[0]
$want = New-ScheduledTaskTrigger -Weekly -DaysOfWeek {day} -At {at}
if ($cur.DaysOfWeek -eq $want.DaysOfWeek -and ([datetime]$cur.StartBoundary).ToString('HH:mm') -eq '{at}') {{ 'same'; exit }}
Set-ScheduledTask -TaskName '{TASK_NAME}' -Trigger $want -ErrorAction Stop | Out-Null
'moved'
""")
    state = r.stdout.strip()
    if r.returncode != 0 or state not in ("none", "same", "moved"):
        return t("ext.usage-watch.snapshot.align_failed", error=r.stderr.strip() or state)
    if state == "none":
        return t("ext.usage-watch.snapshot.schedule_not_registered")
    values = {"day": day, "at": at, "reset": f"{reset:%m-%d %H:%M}", "lead": LEAD_MIN}
    if state == "same":
        return t("ext.usage-watch.snapshot.schedule_kept", **values)
    return t("ext.usage-watch.snapshot.schedule_moved", **values)


def push_to_org_enabled() -> bool:
    """截圖要不要存進組織記憶庫並推上去：設定 usage_watch.snapshot_push_to_org 是 true 才要（出廠 false、沒寫也當 false）。"""
    section = load_config().get("usage_watch")
    return isinstance(section, dict) and section.get("snapshot_push_to_org") is True


def _pick_save_dir(result: dict) -> Path:
    """沒開 snapshot_push_to_org → 存本機。開了：接上組織記憶庫就存它的 usage-snapshots/；沒接上退本機並把原因記進
    result["repo_error"]（不靜默）。"""
    if not push_to_org_enabled():
        result["org_push"] = "off"
        return OUT_DIR
    root = org_memory_root()
    if root is None or not root.is_dir():
        result["repo_error"] = t("ext.usage-watch.snapshot.org_repo_missing", path=OUT_DIR)
        return OUT_DIR
    save_dir = root / REPO_SUBDIR
    save_dir.mkdir(exist_ok=True)
    return save_dir


def _push_to_repo(result: dict) -> None:
    """把組織記憶庫 commit／拉／push 一輪（wg_vcs_sync 主邏輯）；沒推上去記進 result["repo_error"]。
    他 worker 持鎖（locked）不算錯：請求已落檔，由持鎖者補跑。"""
    config = load_config()
    root = org_memory_root().resolve()
    targets = [t for t in collect_sync_targets("", config) if t.root == root]
    if not targets:
        result["repo_error"] = t("ext.usage-watch.snapshot.not_a_repo", root=root)
        return
    res = sync_targets_inline(targets, config, reason="usage-snapshot", log=log)[0]
    result["repo_sync"] = res.get("status")
    if res.get("status") == "locked" or (res.get("status") == "ok" and res.get("pushed")):
        return
    pull_reason = (res.get("pull") or {}).get("reason")
    result["repo_error"] = t("ext.usage-watch.snapshot.push_failed", status=res.get('status'), reason=res.get('reason') or pull_reason or t("ext.usage-watch.snapshot.not_pushed"))


def _finish(result: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LAST_RUN.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")
    if result["ok"]:
        log(f"OK → {result['file']}")
        if result.get("org_push") == "off":
            log(t("ext.usage-watch.snapshot.local_only"))
        if result.get("repo_error"):
            log(f"WARN: {result['repo_error']}")
        for ln in result.get("lines", []):
            log(f"  {ln}")
        if result.get("weekly_used"):
            log(t("ext.usage-watch.snapshot.weekly_used_line", weekly_used=result['weekly_used']))
        log(result["schedule"])
    else:
        log(f"FAILED: {result['error']}" + (f" → {result['file']}" if result["file"] else ""))
    for note in result.get("notes", []):
        log(f"WARN: {note}")


def do_register(at: str, day: str) -> int:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    script = str(Path(__file__).resolve())
    ps = f"""
$a = New-ScheduledTaskAction -Execute '{pythonw}' -Argument '"{script}"'
$t = New-ScheduledTaskTrigger -Weekly -DaysOfWeek {day} -At {at}
$s = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $a -Trigger $t -Settings $s -Force | Out-Null
(Get-ScheduledTaskInfo -TaskName '{TASK_NAME}').NextRunTime
"""
    r = _powershell(ps)
    if r.returncode != 0:
        log(t("ext.usage-watch.snapshot.register_failed", error=r.stderr.strip()))
        return 1
    log(t("ext.usage-watch.snapshot.registered", task=TASK_NAME, day=day, at=at, next_run=r.stdout.strip()))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=t("ext.usage-watch.snapshot.description"), formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--login", action="store_true", help=t("ext.usage-watch.snapshot.login_help"))
    ap.add_argument("--login-timeout", type=int, default=60, metavar="MIN", help=t("ext.usage-watch.snapshot.login_timeout_help"))
    ap.add_argument("--headless", action="store_true", help=t("ext.usage-watch.snapshot.headless_help"))
    ap.add_argument("--register", action="store_true", help=t("ext.usage-watch.snapshot.register_help"))
    ap.add_argument("--at", default="03:30", help=t("ext.usage-watch.snapshot.at_help"))
    ap.add_argument("--day", default="Tuesday", help=t("ext.usage-watch.snapshot.day_help"))
    a = ap.parse_args()
    if a.register:
        return do_register(a.at, a.day)
    if a.login:
        return do_login(a.login_timeout)
    return do_snapshot(headed=not a.headless)


if __name__ == "__main__":
    sys.exit(main())
