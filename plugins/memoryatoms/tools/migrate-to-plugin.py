#!/usr/bin/env python3
"""安裝版 → 插件版的切換（D5：一台只跑一套）。只做這個方向；插件版 → 安裝版不做。

預設只預覽（dry-run），列出會改什麼，不動任何檔。
  --apply  備份 settings.json 到 <Claude 設定目錄>/backups/migrate-<時間戳>/，拿掉安裝版的 hook 條目
           （只拿指向本系統腳本的：安裝器照 templates/settings.template.json 寫進去的那幾支；擴充等其他 hook 留著），
           用 `claude mcp remove workflow-guardian -s user` 拿掉使用者層 MCP 註冊，再印出裝插件的下一步。
  --undo   從最近一次（或 --from 指定的）備份還原 settings.json 與 MCP 註冊。
資料（卡片、索引、Logs）、CLAUDE.md、workflow/config*.json 都不動：插件版把資料根設成原本的 ~/.claude 就接著用。
Claude 設定目錄＝CLAUDE_CONFIG_DIR，沒有就 ~/.claude；使用者層 MCP 註冊在 lib/project_root.claude_global_config()。
MIRROR: hooks/edition-guard.js（「安裝版正在跑」的判斷：workflow-guardian.py 的 hook、名叫 workflow-guardian 的 MCP）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

CODE_DIR = Path(__file__).resolve().parent.parent
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))
from lib.i18n import t  # noqa: E402
from lib.project_root import claude_global_config  # noqa: E402

MCP_NAME = "workflow-guardian"
PLUGIN_ID = "memoryatoms@memoryatoms"
# 安裝器寫進 settings.json 的腳本（templates/settings.template.json 裡 {{CODE_ROOT}}/hooks/ 底下的全部）。
# 只認這幾支：擴充（codex_companion 等）的 hook 照舊留著，它們讀安裝資料夾的程式還能跑。
INSTALL_SCRIPTS = (
    "workflow-guardian.py",
    "ensure-mcp.py",
    "run-bash-hidden.py",
    "plan_bash_guard.py",
    "lang_guard.py",
    "handlers/instructions_loaded.py",
    "handlers/stop_failure.py",
)
_SCRIPT_RE = re.compile(r"/hooks/(" + "|".join(re.escape(s) for s in INSTALL_SCRIPTS) + r")(?![\w.-])", re.IGNORECASE)


def claude_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    return data if isinstance(data, dict) else None


def install_script(command: str) -> str:
    """這條 hook 指令跑的是安裝版的哪支腳本；不是（或是指向插件的）回空字串。"""
    text = command.replace("\\\\", "/").replace("\\", "/")
    if "claude_plugin_root" in text.lower():
        return ""
    m = _SCRIPT_RE.search(text)
    return m.group(1) if m else ""


def _hook_text(h: dict) -> str:
    parts = [h.get("command")] + list(h.get("args") or [])
    return " ".join(p for p in parts if isinstance(p, str))


def plan_settings(settings: dict) -> tuple[dict, list[dict]]:
    """回（拿掉安裝版條目後的 settings、拿掉的清單）。整組 hooks 拿空就拿掉那組，事件拿空就拿掉那個事件。"""
    new = json.loads(json.dumps(settings))
    removed: list[dict] = []
    hooks = new.get("hooks")
    if not isinstance(hooks, dict):
        return new, removed
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for g in groups:
            items = g.get("hooks") if isinstance(g, dict) else None
            if not isinstance(items, list):
                kept_groups.append(g)
                continue
            kept = []
            for h in items:
                script = install_script(_hook_text(h)) if isinstance(h, dict) else ""
                if script:
                    removed.append({"event": event, "matcher": g.get("matcher", ""), "script": script})
                else:
                    kept.append(h)
            if kept:
                g["hooks"] = kept
                kept_groups.append(g)
            elif len(kept) == len(items):  # 本來就空的組原樣留
                kept_groups.append(g)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    return new, removed


def mcp_entry() -> Optional[dict]:
    cfg = _read_json(claude_global_config()) or {}
    servers = cfg.get("mcpServers")
    entry = servers.get(MCP_NAME) if isinstance(servers, dict) else None
    return entry if isinstance(entry, dict) else None


def claude_command(override: str) -> Optional[list[str]]:
    """claude 指令；--claude 指定的優先（.py 用目前的 Python 跑，給測試的假執行器用），否則 lib/wg_platform.find_claude()。"""
    if override:
        return [sys.executable, override] if override.lower().endswith(".py") else [override]
    from lib.wg_platform import find_claude
    found = find_claude()
    return [found] if found else None


def run_claude(cmd: list[str], args: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd + args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return r.returncode, (r.stderr or r.stdout or "").strip()


def write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".migrate.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def dump_settings(settings: dict) -> bytes:
    return (json.dumps(settings, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def next_steps() -> str:
    return t("cli.migrate_to_plugin.next_steps", command=f"claude plugin install {PLUGIN_ID}", data_dir="~/.claude")


def cmd_preview(out: dict, as_json: bool) -> int:
    settings_path = claude_config_dir() / "settings.json"
    settings = _read_json(settings_path) or {}
    _, removed = plan_settings(settings)
    entry = mcp_entry()
    out.update(mode="dry-run", settings=str(settings_path), hooks_to_remove=removed, mcp_to_remove=bool(entry),
               changes=len(removed) + (1 if entry else 0))
    if as_json:
        return 0
    print(t("cli.migrate_to_plugin.preview_header", settings=settings_path))
    for r in removed:
        print(t("cli.migrate_to_plugin.preview_hook", event=r["event"], matcher=r["matcher"] or "-", script=r["script"]))
    if entry:
        print(t("cli.migrate_to_plugin.preview_mcp", name=MCP_NAME, config=claude_global_config()))
    if not out["changes"]:
        print(t("cli.migrate_to_plugin.nothing_to_do"))
    else:
        print(t("cli.migrate_to_plugin.preview_untouched"))
        print(t("cli.migrate_to_plugin.preview_how_apply"))
    return 0


def cmd_apply(out: dict, claude_override: str, as_json: bool) -> int:
    cfg_dir = claude_config_dir()
    settings_path = cfg_dir / "settings.json"
    before = settings_path.read_bytes() if settings_path.exists() else None
    settings = _read_json(settings_path) or {}
    new_settings, removed = plan_settings(settings)
    entry = mcp_entry()
    out.update(mode="apply", settings=str(settings_path), hooks_removed=len(removed), mcp_removed=0, backup="")
    if not removed and not entry:
        if not as_json:
            print(t("cli.migrate_to_plugin.nothing_to_do"))
        return 0
    cmd = None
    if entry:
        cmd = claude_command(claude_override)
        if not cmd:
            out["error"] = "claude-not-found"
            print(t("cli.migrate_to_plugin.claude_not_found", name=MCP_NAME), file=sys.stderr)
            return 1
    backup = cfg_dir / "backups" / time.strftime("migrate-%Y%m%d-%H%M%S")
    n = 1
    while backup.exists():  # 同一秒跑兩次
        backup = backup.with_name(f"{backup.name.split('~')[0]}~{n}")
        n += 1
    backup.mkdir(parents=True)
    if before is not None:
        (backup / "settings.json").write_bytes(before)
    manifest = {"settings_path": str(settings_path), "settings_existed": before is not None,
                "settings_sha256_before": _sha(before) if before is not None else "",
                "hooks_removed": removed, "mcp_name": MCP_NAME, "mcp_entry": entry, "mcp_removed": False,
                "global_config": str(claude_global_config())}
    (backup / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8",
                                         newline="\n")
    out["backup"] = str(backup)
    if entry:  # 先拿 MCP：失敗就什麼設定都還沒動
        rc, msg = run_claude(cmd, ["mcp", "remove", MCP_NAME, "-s", "user"])
        if rc != 0:
            out["error"] = "mcp-remove-failed"
            print(t("cli.migrate_to_plugin.mcp_remove_failed", name=MCP_NAME, error=msg), file=sys.stderr)
            return 1
        manifest["mcp_removed"] = True
        out["mcp_removed"] = 1
    after = before
    if removed:
        after = dump_settings(new_settings)
        write_bytes_atomic(settings_path, after)
    manifest["settings_sha256_after"] = _sha(after) if after is not None else ""
    (backup / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8",
                                         newline="\n")
    if not as_json:
        print(t("cli.migrate_to_plugin.applied", hooks=len(removed), mcp=out["mcp_removed"], backup=backup))
        print(next_steps())
        print(t("cli.migrate_to_plugin.how_undo"))
    return 0


def latest_backup(cfg_dir: Path) -> Optional[Path]:
    root = cfg_dir / "backups"
    if not root.is_dir():
        return None
    cands = [p for p in root.iterdir()
             if p.name.startswith("migrate-") and (p / "manifest.json").is_file() and not (p / "undone.json").exists()]
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


def cmd_undo(out: dict, src: str, force: bool, claude_override: str, as_json: bool) -> int:
    cfg_dir = claude_config_dir()
    backup = Path(src) if src else latest_backup(cfg_dir)
    out.update(mode="undo", backup=str(backup or ""), settings_restored=0, mcp_restored=0)
    if not backup or not (backup / "manifest.json").is_file():
        out["error"] = "no-backup"
        print(t("cli.migrate_to_plugin.no_backup", dir=cfg_dir / "backups"), file=sys.stderr)
        return 1
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    settings_path = Path(manifest["settings_path"])
    now = settings_path.read_bytes() if settings_path.exists() else None
    expect = manifest.get("settings_sha256_after", "")
    if not force and expect and (now is None or _sha(now) != expect):  # 切換後使用者又改過設定：還原會蓋掉那些改動
        out["error"] = "settings-changed"
        print(t("cli.migrate_to_plugin.changed_since", settings=settings_path), file=sys.stderr)
        return 1
    if manifest.get("mcp_removed") and manifest.get("mcp_entry"):
        cmd = claude_command(claude_override)
        if not cmd:
            out["error"] = "claude-not-found"
            print(t("cli.migrate_to_plugin.claude_not_found", name=MCP_NAME), file=sys.stderr)
            return 1
        rc, msg = run_claude(cmd, ["mcp", "add-json", manifest["mcp_name"],
                                   json.dumps(manifest["mcp_entry"], ensure_ascii=False), "-s", "user"])
        if rc != 0:
            out["error"] = "mcp-add-failed"
            print(t("cli.migrate_to_plugin.mcp_add_failed", name=MCP_NAME, error=msg), file=sys.stderr)
            return 1
        out["mcp_restored"] = 1
    if manifest.get("settings_existed"):
        write_bytes_atomic(settings_path, (backup / "settings.json").read_bytes())
        out["settings_restored"] = 1
    (backup / "undone.json").write_text(json.dumps({"undone_at": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n",
                                        encoding="utf-8", newline="\n")
    if not as_json:
        print(t("cli.migrate_to_plugin.undone", backup=backup))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=t("cli.migrate_to_plugin.description"))
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help=t("cli.migrate_to_plugin.arg_apply"))
    mode.add_argument("--undo", action="store_true", help=t("cli.migrate_to_plugin.arg_undo"))
    ap.add_argument("--from", dest="src", default="", help=t("cli.migrate_to_plugin.arg_from"))
    ap.add_argument("--force", action="store_true", help=t("cli.migrate_to_plugin.arg_force"))
    ap.add_argument("--claude", default="", help=t("cli.migrate_to_plugin.arg_claude"))
    ap.add_argument("--json", action="store_true", help=t("cli.migrate_to_plugin.arg_json"))
    a = ap.parse_args(argv)
    out: dict = {}
    if a.apply:
        rc = cmd_apply(out, a.claude, a.json)
    elif a.undo:
        rc = cmd_undo(out, a.src, a.force, a.claude, a.json)
    else:
        rc = cmd_preview(out, a.json)
    if a.json:
        print(json.dumps(out, ensure_ascii=False))
    return rc


if __name__ == "__main__":
    sys.exit(main())
