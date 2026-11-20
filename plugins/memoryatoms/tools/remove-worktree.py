"""remove-worktree.py — 收掉一個 git 工作樹，並一起取消它的背景同步登記。

做什麼：
  給一個 git 工作樹的資料夾路徑 → 用 `git worktree remove` 移除（有未提交的改動就拒絕，除非加 --force）
  → 把 workflow/vcs-sync 裡這個資料夾的登記、殘留請求、標記、鎖檔一起清掉 → 印一行結果。
  資料夾已經不在了（先被手動刪掉）也照樣把登記清掉：缺哪一邊，就處理還在的那一邊。
  AI 要移除工作樹時走這支，不自己拼 rm／git 指令。

怎麼跑：
  python <記憶系統>/tools/remove-worktree.py <工作樹路徑> [--force]
  結果尾行：REMOVE_WORKTREE OK removed|gone ...（成功）或 REMOVE_WORKTREE REFUSED|ERROR ...（exit 1，登記原樣保留）。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "hooks"))
from lib.i18n import t  # noqa: E402

for _stream in (sys.stdout, sys.stderr):   # Windows 預設編碼印不出中文
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def _git(*args: str, cwd: str = "."):
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def _forget(path: str) -> None:
    import wg_vcs_sync as vs
    seen = []
    for cand in (Path(path), Path(path).resolve()):
        if cand.as_posix() not in seen:
            seen.append(cand.as_posix())
            vs.forget_root(cand)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="remove-worktree.py", description=t("cli.remove_worktree.description"))
    ap.add_argument("path", help=t("cli.remove_worktree.arg_path"))
    ap.add_argument("--force", action="store_true", help=t("cli.remove_worktree.arg_force"))
    args = ap.parse_args(argv)
    path = os.path.abspath(args.path)

    if not Path(path).exists():
        _forget(path)
        print(t("cli.remove_worktree.gone", path=Path(path).as_posix()))
        return 0

    rc, common = _git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=path)
    if rc != 0:
        print(t("cli.remove_worktree.not_a_worktree", path=Path(path).as_posix(), why=common[:200]))
        return 1
    cmd = ["worktree", "remove"] + (["--force"] if args.force else []) + [path]
    rc, out = _git(*cmd, cwd=common)
    if rc != 0:
        print(t("cli.remove_worktree.refused", path=Path(path).as_posix(), why=out[:300]))
        return 1
    _forget(path)
    print(t("cli.remove_worktree.removed", path=Path(path).as_posix()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
