#!/usr/bin/env python3
"""codex-run.py：照 codex skill 組 Codex 提示詞、帶對的沙箱去跑、跑前跑後比對版控狀態、原始輸出全文存檔。

做什麼：
  new：把 assets/<模式>-prompt.md 套上 assets/sync-package.md，第 2 段自動原文照貼使用者的尺
       （IDENTITY〈設定衝突與程式需求〉、coding-style 開頭的優先序＋〈設計取捨的尺〉、code-thinking 全文；標題降兩級、內文不改）。
       其餘「〔待填」留給 CC 填。
  run：提示詞還有「〔待填」或 --cwd 不存在就拒跑；沙箱照模式固定（review→read-only、build→workspace-write），
       不給 danger-full-access；跑前跑後記 --guard 目錄的 git／svn status（review 模式自動把 --cwd 加進去），有變就 exit 1；
       同行程呼叫 tools/codex-call.py 的 run_codex（不經子行程 stdout，免得 Windows 管線轉成 cp950）；
       答案存 <out-dir>/codex-output.md，codex 自己的 stderr（讀了哪些檔、跑了哪些指令）存 codex-stderr.txt，前後狀態存 guard-*.txt。
怎麼跑：
  python codex-run.py new --mode review --out <暫存夾>/prompt.md
  python codex-run.py run --mode review --prompt-file <prompt.md> --cwd <repo> --out-dir <暫存夾> [--guard <別的 repo>]...
  python codex-run.py run --mode build  --prompt-file <prompt.md> --cwd <工作樹> --out-dir <暫存夾> --guard <live repo>
  尾行：CODEX_RUN_NEW DONE out=… ／ CODEX_RUN DONE mode=… sandbox=… secs=… chars=… ok=0|1 guard_changed=…
  完整參數以 --help 為準。
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

SKILL_DIR = Path(__file__).resolve().parents[1]
# 程式碼根：skill 放在 extensions/codex/skills/（原生插件就地載入），或被複製進 skills/，往上找到有 hooks/workflow-guardian.py 的那層
CODE_DIR = next(p for p in SKILL_DIR.parents if (p / "hooks" / "workflow-guardian.py").is_file())
sys.path.insert(0, str(CODE_DIR))
from lib.project_root import claude_data_dir  # noqa: E402
from lib.i18n import t  # noqa: E402

CODEX_CALL = CODE_DIR / "extensions" / "codex" / "tools" / "codex-call.py"
SANDBOX_BY_MODE = {"review": "read-only", "build": "workspace-write"}
TODO_MARK = "〔待填"
RULER_MARK = "〔自動貼：使用者的尺〕"
PACKAGE_MARK = "〔同步包〕"


def section(text: str, heading: str, path: Path) -> str:
    """取「## heading」那一段到下一個「## 」為止；找不到就明說，不默默少貼。"""
    m = re.search(rf"^## {re.escape(heading)}.*?(?=^## |\Z)", text, flags=re.M | re.S)
    if not m:
        raise SystemExit(t("ext.codex.run.section_missing", path=path, heading=heading))
    return m.group(0).strip()


def strip_frontmatter(text: str) -> str:
    return re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S).strip()


def demote(text: str) -> str:
    """標題降兩級，免得跟同步包自己的「## 1.」段落搶層級；內文一字不動。"""
    return re.sub(r"^(#{1,4}) ", lambda m: "##" + m.group(1) + " ", text, flags=re.M)


def ruler_text() -> str:
    identity = claude_data_dir() / "IDENTITY.md"
    style = CODE_DIR / "rules" / "coding-style.md"
    thinking = CODE_DIR / "rules" / "code-thinking.md"
    style_text = strip_frontmatter(style.read_text(encoding="utf-8"))
    priority_mark = "> 優先序"   # 使用者規則檔裡那一行的開頭（比對用，不翻）
    priority = next((ln for ln in style_text.splitlines() if ln.startswith(priority_mark)), "")
    if not priority:
        raise SystemExit(t("ext.codex.run.priority_line_missing", style=style, marker=priority_mark))
    parts = []
    if identity.is_file():
        parts.append("（IDENTITY.md〈設定衝突與程式需求〉）\n" +
                     demote(section(identity.read_text(encoding="utf-8"), "設定衝突與程式需求", identity)))
    else:
        print(t("ext.codex.run.identity_missing", identity=identity, section="設定衝突與程式需求"), file=sys.stderr)
    parts.append("（rules/coding-style.md 開頭的優先序＋〈設計取捨的尺〉全段）\n" + priority + "\n\n" +
                 demote(section(style_text, "設計取捨的尺", style)))
    parts.append("（rules/code-thinking.md 全文）\n" + demote(strip_frontmatter(thinking.read_text(encoding="utf-8"))))
    return "\n\n".join(parts)


def cmd_new(a) -> int:
    package = (SKILL_DIR / "assets" / "sync-package.md").read_text(encoding="utf-8")
    prompt = (SKILL_DIR / "assets" / f"{a.mode}-prompt.md").read_text(encoding="utf-8")
    text = prompt.replace(PACKAGE_MARK, package.replace(RULER_MARK, ruler_text()).strip())
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")
    print(t("ext.codex.run.todo_count", count=text.count(TODO_MARK), mark=TODO_MARK))
    print(f"CODEX_RUN_NEW DONE out={out.as_posix()}")
    return 0


def vcs_status(d: Path) -> bytes:
    """最近一層 .svn 或 .git 決定用哪種版控（svn 工作副本放在 git repo 底下時，要比的是 svn），
    回完整狀態（含沒追蹤的檔）；都找不到就回一行說明，讓比對結果看得出「沒比到」。"""
    for p in (d, *d.parents):
        if (p / ".svn").exists():
            return b"[svn]\n" + subprocess.run(["svn", "status", str(d)], capture_output=True).stdout
        if (p / ".git").exists():
            return b"[git]\n" + subprocess.run(["git", "-C", str(d), "status", "--porcelain"], capture_output=True).stdout
    return f"[無版控，無法比對] {d}\n".encode("utf-8")


def snapshot(guards: list, out_dir: Path, tag: str) -> dict:
    states = {}
    for i, g in enumerate(guards):
        states[g] = vcs_status(g)
        (out_dir / f"guard-{tag}-{i}.txt").write_bytes(str(g).encode("utf-8") + b"\n" + states[g])
    return states


def cmd_run(a) -> int:
    prompt_file, cwd, out_dir = Path(a.prompt_file), Path(a.cwd), Path(a.out_dir)
    prompt = prompt_file.read_text(encoding="utf-8")
    if TODO_MARK in prompt:
        print(t("ext.codex.run.refuse_unfilled", prompt_file=prompt_file, count=prompt.count(TODO_MARK), mark=TODO_MARK), file=sys.stderr)
        return 1
    if not cwd.is_dir():   # codex-call 遇到不存在的 cwd 會改用目前目錄，不報錯
        print(t("ext.codex.run.refuse_cwd_missing", cwd=cwd), file=sys.stderr)
        return 1
    guards = [Path(g) for g in (a.guard or [])]
    if a.mode == "review" and cwd not in guards:
        guards.insert(0, cwd)
    if a.mode == "build" and not guards:
        print(t("ext.codex.run.refuse_build_without_guard"), file=sys.stderr)
        return 1
    sandbox = a.sandbox or SANDBOX_BY_MODE[a.mode]
    out_dir.mkdir(parents=True, exist_ok=True)

    # 同行程呼叫 codex-call 的 run_codex：答案不經 stdout 轉碼（子行程管線在 Windows 走 cp950），
    # 成功時也拿得到 codex 自己的 stderr（讀了哪些檔、跑了哪些指令），裁決時要看
    spec = importlib.util.spec_from_file_location("codex_call", CODEX_CALL)
    codex_call = importlib.util.module_from_spec(spec)
    sys.modules["codex_call"] = codex_call   # 檔內的 @dataclass 要從 sys.modules 找得到自己的模組
    spec.loader.exec_module(codex_call)
    cfg = codex_call.load_config()

    before = snapshot(guards, out_dir, "before")
    started = time.monotonic()
    res = codex_call.run_codex(prompt, config=cfg, cwd=str(cwd), model=str(cfg.get("model") or ""),
                               timeout_s=a.timeout, sandbox=sandbox)
    secs = round(time.monotonic() - started)
    (out_dir / "codex-output.md").write_text(res.text, encoding="utf-8", newline="\n")
    (out_dir / "codex-stderr.txt").write_text(res.stderr, encoding="utf-8", newline="\n")
    after = snapshot(guards, out_dir, "after")

    changed = [str(g) for g in guards if before[g] != after[g]]
    for g in changed:
        print(t("ext.codex.run.guard_changed", path=g, out_dir=out_dir),
              file=sys.stderr)
    if not res.ok:
        print(t("ext.codex.run.codex_failed", reason=res.reason, stderr_file=out_dir / 'codex-stderr.txt'), file=sys.stderr)
    print(t("ext.codex.run.output_path", output=out_dir / 'codex-output.md'))
    print(f"CODEX_RUN DONE mode={a.mode} sandbox={sandbox} secs={secs} chars={len(res.text)} "
          f"ok={int(res.ok)} guard_changed={len(changed)}")
    return 0 if res.ok and not changed else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=t("ext.codex.run.description"), formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new", help=t("ext.codex.run.new_help"))
    n.add_argument("--mode", choices=SANDBOX_BY_MODE, required=True)
    n.add_argument("--out", required=True, help=t("ext.codex.run.out_help"))
    r = sub.add_parser("run", help=t("ext.codex.run.run_help"))
    r.add_argument("--mode", choices=SANDBOX_BY_MODE, required=True)
    r.add_argument("--prompt-file", required=True)
    r.add_argument("--cwd", required=True, help=t("ext.codex.run.cwd_help"))
    r.add_argument("--out-dir", required=True, help=t("ext.codex.run.out_dir_help"))
    r.add_argument("--guard", action="append", help=t("ext.codex.run.guard_help"))
    r.add_argument("--sandbox", choices=("read-only", "workspace-write"),
                   help=t("ext.codex.run.sandbox_help"))
    r.add_argument("--timeout", type=int, default=1800, help=t("ext.codex.run.timeout_help"))
    a = ap.parse_args()
    try:
        return cmd_new(a) if a.cmd == "new" else cmd_run(a)
    except (OSError, UnicodeDecodeError) as e:
        print(t("ext.codex.run.failed", error=e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
