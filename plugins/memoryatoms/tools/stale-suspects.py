#!/usr/bin/env python3
"""stale-suspects — 產生「疑似過期」清單，放在每一層 .claude 底下讓人一眼看到。

做什麼：把「疑似已過時、但系統無法確定」的記憶卡寫成白話清單（一卡一行：路徑＋原因），檔尾附一段可直接貼給 AI 的修正 prompt。
  來源有三種（判法全部重用 tools/atom-health-check.py 與 lib/atom_feedback.py 的現成函式，這裡不另寫一份）：
    1. 相依檔已不存在（check_stale_deps）
    2. 相依檔在卡片最後驗證日之後改過（check_depends_mtime）
    3. 被 AI 投過「誤導」票（<記憶層>/_votes/*.jsonl 的 misleading）
  不碰任何卡片；注入端不加標記；個人隱私卡（personal/<人>/secret/）與別人的 personal 卡一律不列。
  清單為空就刪掉檔，不留空檔。檔名跟著語言（zh-TW「疑似過期.md」、en「stale-suspects.md」），換語言時舊語言的檔一併清掉。

放哪：根層＝資料根本身（安裝版就是 ~/.claude）；專案層＝<專案根>/.claude/。檔案是自動產生的，不進版控。

怎麼跑：
  python tools/stale-suspects.py                      只掃根層
  python tools/stale-suspects.py --project <專案根>    再掃該專案層（可重複給）
  python tools/stale-suspects.py --skip-root --project <專案根>
資料根由環境變數 WG_CLAUDE_DIR 決定（沒設就是 ~/.claude），與其他工具同一算法。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE_DIR))
from lib.i18n import t  # noqa: E402
from lib.project_root import claude_data_dir  # noqa: E402

FILENAME_KEY = "cli.stale_suspects.filename"   # 語言表直接查表時用；翻譯一律寫字面 t("cli.stale_suspects.filename")
LANGUAGES = ("zh-TW", "en")


def _load_health_check():
    """tools/atom-health-check.py 檔名有連字號，不能直接 import：用路徑載入，沿用它的偵測函式。"""
    spec = importlib.util.spec_from_file_location("atom_health_check", CODE_DIR / "tools" / "atom-health-check.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["atom_health_check"] = mod
    spec.loader.exec_module(mod)
    return mod


def _all_filenames() -> set[str]:
    """兩種語言的檔名（清除舊語言留下的檔用）。"""
    import lib.i18n as i18n
    names = {t("cli.stale_suspects.filename")}
    for lang in LANGUAGES:
        try:
            table = json.loads((i18n.TABLE_DIR / f"{lang}.json").read_text(encoding="utf-8"))
            if FILENAME_KEY in table:
                names.add(table[FILENAME_KEY])
        except (OSError, ValueError):
            pass
    return names


def _skip_card(rel_parts: tuple[str, ...], me: str) -> bool:
    """個人隱私卡不列：personal/<人>/…/secret/…、別人的 personal 卡。"""
    if not rel_parts or rel_parts[0] != "personal":
        return False
    if "secret" in rel_parts[:-1]:
        return True
    return len(rel_parts) > 1 and rel_parts[1].lower() != me.lower()


def _misleading_votes(memory_dir: Path) -> dict[str, int]:
    """<記憶層>/_votes/*.jsonl 裡 misleading 票數，鍵是卡片相對記憶層的 posix 路徑（同一張票只算一次）。"""
    counts: dict[str, int] = {}
    seen: set[str] = set()
    votes = memory_dir / "_votes"
    if not votes.is_dir():
        return counts
    for f in sorted(votes.glob("*.jsonl")):
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError as e:
            print(f"[stale-suspects] votes unreadable, skipped: {f}: {e}", file=sys.stderr)
            continue
        for line in lines:
            try:
                v = json.loads(line)
            except ValueError:
                continue
            if v.get("verdict") != "misleading" or not v.get("path_rel"):
                continue
            vid = str(v.get("id") or f"{f.name}:{line}")
            if vid in seen:
                continue
            seen.add(vid)
            counts[v["path_rel"]] = counts.get(v["path_rel"], 0) + 1
    return counts


def collect(hc, memory_dir: Path, me: str) -> dict[str, list[str]]:
    """一層記憶的疑似過期卡：{相對記憶層的路徑: [原因, ...]}。偵測交給 atom-health-check。"""
    hc.use_memory_root(memory_dir)
    hc.MEMORY_ROOT = memory_dir
    atoms = hc.find_atoms(memory_dir)

    def rel_of(p: str | Path) -> str | None:
        try:
            parts = Path(p).resolve().relative_to(memory_dir.resolve()).parts
        except ValueError:
            return None
        return None if _skip_card(parts, me) else "/".join(parts)

    atoms = {n: p for n, p in atoms.items() if rel_of(p) is not None}
    reasons: dict[str, list[str]] = {}

    def add(path: str | Path, text: str) -> None:
        r = rel_of(path)
        if r is not None:
            reasons.setdefault(r, []).append(text)

    for row in hc.check_stale_deps(atoms):
        add(row["file"], t("cli.stale_suspects.reason_missing", dep=row["dep"]))
    for row in hc.check_depends_mtime(atoms):
        add(row["file"], t("cli.stale_suspects.reason_newer", dep=row["dep"],
                           verified=row["verified_at"], changed=row["dep_changed"]))
    allowed = {rel_of(p) for p in atoms.values()}
    for rel, n in _misleading_votes(memory_dir).items():
        if rel in allowed:
            reasons.setdefault(rel, []).append(t("cli.stale_suspects.reason_misleading", n=n))
    return reasons


def render(reasons: dict[str, list[str]], claude_dir: Path, filename: str) -> str:
    sep = t("cli.stale_suspects.reason_sep")
    lines = [t("cli.stale_suspects.title"), "", t("cli.stale_suspects.intro"), "",
             t("cli.stale_suspects.count", n=len(reasons)), ""]
    for rel in sorted(reasons):
        lines.append(f"- `memory/{rel}`：{sep.join(reasons[rel])}")
    prompt = t("cli.stale_suspects.prompt_body", file=filename, dir=str(claude_dir))
    lines += ["", t("cli.stale_suspects.prompt_title"), "", "```text", prompt, "```", ""]
    return "\n".join(lines)


def write_layer(hc, claude_dir: Path, me: str) -> int:
    """掃 <claude_dir>/memory，把清單寫到 claude_dir；空就刪。回疑似過期卡數。"""
    memory_dir = claude_dir / "memory"
    if not memory_dir.is_dir():
        print(t("cli.stale_suspects.no_memory", path=str(memory_dir)), file=sys.stderr)
        return 0
    reasons = collect(hc, memory_dir, me)
    target = claude_dir / t("cli.stale_suspects.filename")
    for name in _all_filenames():          # 換語言後舊語言的檔不留
        if name != target.name:
            (claude_dir / name).unlink(missing_ok=True)
    if not reasons:
        if target.exists():
            target.unlink()
            print(t("cli.stale_suspects.removed", path=str(target)))
        else:
            print(t("cli.stale_suspects.clean", layer=str(claude_dir)))
        return 0
    target.write_text(render(reasons, claude_dir, target.name), encoding="utf-8", newline="\n")
    print(t("cli.stale_suspects.wrote", path=str(target), n=len(reasons)))
    return len(reasons)


def main() -> int:
    ap = argparse.ArgumentParser(description=t("cli.stale_suspects.description"))
    ap.add_argument("--project", action="append", default=[], metavar="ROOT", help=t("cli.stale_suspects.help_project"))
    ap.add_argument("--skip-root", action="store_true", help=t("cli.stale_suspects.help_skip_root"))
    args = ap.parse_args()

    from lib.atom_feedback import _user
    me = _user()
    hc = _load_health_check()
    layers = ([] if args.skip_root else [claude_data_dir()]) + [Path(p) / ".claude" for p in args.project]
    for layer in layers:
        write_layer(hc, layer, me)
    return 0


if __name__ == "__main__":
    sys.exit(main())
