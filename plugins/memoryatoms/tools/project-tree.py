#!/usr/bin/env python3
"""project-tree.py — 專案根宣告檔（.claude/project-tree.json）的查看與增刪改。

做什麼：子專案的 Claude session 要把記憶歸到核心根層，靠各層 `.claude/project-tree.json` 宣告
（根層列 subs、子層指 root）。hook 只讀這些檔，所有寫入都走本工具或手改 JSON。
怎麼跑（--cwd 預設目前目錄；寫入子命令皆可 --dry-run 只印不寫）：
  show                         本層與上層宣告檔內容 + 目前生效的解析
  explain [<cwd>] [--json]     解析過程：hops / claimed_by / candidates / warnings / infos
  set-root <path> [--abs]      本層 root（相對化寫入；--abs 另寫 root_abs 供本機用）
  unset-root                   移除本層 root / root_abs
  add-sub <rel>...             本層 subs 增加（冪等）
  remove-sub <rel>...          本層 subs 移除
  standalone on|off            本層獨立（停止向上認領、不再提問）
  claim --root <path> [--sub-path <rel>] [--both]
                               雙向一次完成：根層 subs 追加本層在根層下的第一段路徑；
                               本層已有 .claude/ 才同時寫 root（--both 強制寫）
  pick                         彈原生資料夾視窗選根層，選定後等同 claim --root <選定>
完整參數以 --help 為準。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

CODE_DIR = Path(__file__).resolve().parent.parent
if str(CODE_DIR / "lib") not in sys.path:
    sys.path.insert(0, str(CODE_DIR / "lib"))

from project_root import (  # noqa: E402
    DECL_NAME, clear_cache, declaration_path, describe, load_declaration, resolve_project_root,
)
from i18n import t  # noqa: E402
from wg_platform import paste_command  # noqa: E402

ORDER = ("root", "root_abs", "subs", "standalone")


# ─── 讀寫 ─────────────────────────────────────────────────────────────────────


def _read_raw(layer: Path) -> Dict[str, Any]:
    p = declaration_path(layer)
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        sys.exit(t("cli.project_tree.invalid_json", path=p, error=e))
    if not isinstance(raw, dict):
        sys.exit(t("cli.project_tree.not_object", path=p))
    return raw


def _dump(raw: Dict[str, Any]) -> str:
    ordered = {k: raw[k] for k in ORDER if k in raw}
    ordered.update({k: v for k, v in raw.items() if k not in ordered})
    return json.dumps(ordered, ensure_ascii=False, indent=2) + "\n"


def _write_raw(layer: Path, raw: Dict[str, Any], dry_run: bool) -> None:
    """只動呼叫端改過的欄位，未知鍵原樣保留；tmp + 原子 replace。"""
    p = declaration_path(layer)
    before = p.read_text(encoding="utf-8-sig") if p.is_file() else ""
    after = _dump(raw)
    print(f"{'[dry-run] ' if dry_run else ''}{p}")
    if before.strip() == after.strip():
        print(t("cli.project_tree.no_change"))
        return
    if before:
        print(t("cli.project_tree.before") + before.strip().replace("\n", "\n      "))
    print(t("cli.project_tree.after") + after.strip().replace("\n", "\n      "))
    if dry_run:
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(after, encoding="utf-8", newline="\n")
    os.replace(tmp, p)
    clear_cache()


def _layer(args) -> Path:
    return Path(os.path.normpath(os.path.abspath(args.cwd or os.getcwd())))


def _rel_from(layer: Path, target: Path) -> str:
    return Path(os.path.relpath(str(target), str(layer))).as_posix()


def _is_ancestor(target: Path, layer: Path) -> bool:
    t, l = target.resolve(), layer.resolve()
    return t != l and t in l.parents


# ─── 子命令 ───────────────────────────────────────────────────────────────────


def cmd_show(args) -> None:
    layer = _layer(args)
    print(f"WhoAmI : {layer}")
    p = layer
    seen_any = False
    while True:
        d = load_declaration(p)
        if d is not None:
            seen_any = True
            state = t("cli.project_tree.valid") if d.valid else t("cli.project_tree.invalid", error=d.error)
            print(f"\n{d.path}  [{state}]")
            print("  " + (d.path.read_text(encoding="utf-8-sig").strip().replace("\n", "\n  ") or "{}"))
        if p.parent == p or p.resolve() == Path.home().resolve():
            break
        p = p.parent
    if not seen_any:
        print(t("cli.project_tree.no_declaration"))
    print(t("cli.project_tree.effective"))
    res = resolve_project_root(str(layer))
    for line in describe(res, str(layer)):
        print("  " + line)


def cmd_explain(args) -> None:
    cwd = os.path.abspath(args.path or args.cwd or os.getcwd())
    res = resolve_project_root(cwd)
    if args.json:
        payload = {
            "cwd": cwd, "root": str(res.path) if res.path else None, "claimed_by": res.claimed_by,
            "via": str(res.via) if res.via else None, "nearest": str(res.nearest) if res.nearest else None,
            "candidates": [str(c) for c in res.candidates], "hops": [str(h) for h in res.hops],
            "standalone": res.standalone, "fork_atoms": res.fork_atoms, "fingerprint": res.fingerprint,
            "warnings": res.warnings, "infos": res.infos,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    for line in describe(res, cwd):
        print(line)


def cmd_set_root(args) -> None:
    layer = _layer(args)
    target = Path(os.path.normpath(os.path.abspath(args.path)))
    if not target.is_dir():
        sys.exit(t("cli.project_tree.not_dir", path=target))
    raw = _read_raw(layer)
    if _is_ancestor(target, layer):
        raw["root"] = _rel_from(layer, target)
        if args.abs:
            raw["root_abs"] = str(target)
        else:
            raw.pop("root_abs", None)
    else:
        if not args.abs:
            sys.exit(t("cli.project_tree.root_not_ancestor", target=target, layer=layer))
        raw.pop("root", None)
        raw["root_abs"] = str(target)
    _write_raw(layer, raw, args.dry_run)
    _after_write_hint()


def cmd_unset_root(args) -> None:
    layer = _layer(args)
    raw = _read_raw(layer)
    raw.pop("root", None)
    raw.pop("root_abs", None)
    _write_raw(layer, raw, args.dry_run)
    _after_write_hint()


def _norm_sub(value: str) -> str:
    return "*" if value == "*" else Path(value).as_posix().strip("/")


def cmd_add_sub(args) -> None:
    layer = _layer(args)
    raw = _read_raw(layer)
    subs: List[str] = list(raw.get("subs") or [])
    for v in args.rel:
        s = _norm_sub(v)
        if s != "*" and (os.path.isabs(s) or ".." in Path(s).parts):
            sys.exit(t("cli.project_tree.bad_sub", value=repr(v)))
        if s not in subs:
            subs.append(s)
    raw["subs"] = subs
    _write_raw(layer, raw, args.dry_run)
    _after_write_hint()


def cmd_remove_sub(args) -> None:
    layer = _layer(args)
    raw = _read_raw(layer)
    subs: List[str] = list(raw.get("subs") or [])
    wanted = {_norm_sub(v) for v in args.rel}
    raw["subs"] = [s for s in subs if _norm_sub(s) not in wanted]
    _write_raw(layer, raw, args.dry_run)
    _after_write_hint()


def cmd_standalone(args) -> None:
    layer = _layer(args)
    raw = _read_raw(layer)
    if args.mode == "on":
        raw["standalone"] = True
    else:
        raw.pop("standalone", None)
    _write_raw(layer, raw, args.dry_run)
    _after_write_hint()


def _claim(layer: Path, root: Path, sub_path: Optional[str], both: bool, dry_run: bool) -> None:
    if not root.is_dir():
        sys.exit(t("cli.project_tree.not_dir", path=root))
    if not _is_ancestor(root, layer):
        sys.exit(t("cli.project_tree.claim_not_ancestor", root=root, layer=layer))
    rel_to_root = Path(os.path.relpath(str(layer), str(root)))
    sub = _norm_sub(sub_path) if sub_path else rel_to_root.parts[0]
    sub_layer = root / sub
    between = sub_layer.is_dir() and (_is_ancestor(sub_layer, layer) or sub_layer.resolve() == layer.resolve())
    if not between:
        sys.exit(t("cli.project_tree.sub_path_outside", sub=repr(sub), root=root, layer=layer))
    # 根層：subs 追加
    raw_root = _read_raw(root)
    subs: List[str] = list(raw_root.get("subs") or [])
    if "*" not in subs and sub not in subs:
        subs.append(sub)
    raw_root["subs"] = subs
    _write_raw(root, raw_root, dry_run)
    # 子層：已有 .claude/ 才寫 root（不散落新目錄）；--both 強制
    if (sub_layer / ".claude").is_dir() or both:
        raw_sub = _read_raw(sub_layer)
        raw_sub["root"] = _rel_from(sub_layer, root)
        _write_raw(sub_layer, raw_sub, dry_run)
    else:
        print(t("cli.project_tree.no_sub_claude", path=sub_layer))
    _after_write_hint()
    if not dry_run:
        res = resolve_project_root(str(layer))
        print(t("cli.project_tree.resolved", layer=layer, path=res.path, by=res.claimed_by))


def cmd_claim(args) -> None:
    _claim(_layer(args), Path(os.path.normpath(os.path.abspath(args.root))), args.sub_path, args.both, args.dry_run)


def cmd_pick(args) -> None:
    layer = _layer(args)
    res = resolve_project_root(str(layer))
    initial = str(res.candidates[0]) if res.candidates else str(layer.parent)
    try:
        if os.environ.get("PROJECT_TREE_NO_GUI"):      # 測試／headless 明示關閉視窗
            raise RuntimeError(t("cli.project_tree.no_gui_set"))
        import tkinter
        from tkinter import filedialog
        tk = tkinter.Tk()
        tk.withdraw()
        tk.attributes("-topmost", True)
        chosen = filedialog.askdirectory(title=t("cli.project_tree.pick_title", layer=layer), initialdir=initial, mustexist=True)
        tk.destroy()
    except Exception as e:  # 無桌面 / 無 tkinter
        sys.exit(t("cli.project_tree.gui_failed", error=e, layer=layer, cmd=paste_command(Path(__file__).resolve())))
    if not chosen:
        print(t("cli.project_tree.cancelled"))
        return
    _claim(layer, Path(os.path.normpath(chosen)), args.sub_path, args.both, args.dry_run)


def _after_write_hint() -> None:
    print(t("cli.project_tree.after_write_hint"))


# ─── 入口 ─────────────────────────────────────────────────────────────────────


def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(prog="project-tree.py", description=t("cli.project_tree.description"),
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cwd", help=t("cli.project_tree.cwd"))
    ap.add_argument("--dry-run", action="store_true", help=t("cli.project_tree.dry_run"))
    sp = ap.add_subparsers(dest="cmd", required=True)

    sp.add_parser("show", help=t("cli.project_tree.sub_show")).set_defaults(fn=cmd_show)
    p = sp.add_parser("explain", help=t("cli.project_tree.sub_explain"))
    p.add_argument("path", nargs="?")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_explain)
    p = sp.add_parser("set-root", help=t("cli.project_tree.sub_set_root"))
    p.add_argument("path")
    p.add_argument("--abs", action="store_true", help=t("cli.project_tree.abs"))
    p.set_defaults(fn=cmd_set_root)
    sp.add_parser("unset-root", help=t("cli.project_tree.sub_unset_root")).set_defaults(fn=cmd_unset_root)
    p = sp.add_parser("add-sub", help=t("cli.project_tree.sub_add_sub"))
    p.add_argument("rel", nargs="+")
    p.set_defaults(fn=cmd_add_sub)
    p = sp.add_parser("remove-sub", help=t("cli.project_tree.sub_remove_sub"))
    p.add_argument("rel", nargs="+")
    p.set_defaults(fn=cmd_remove_sub)
    p = sp.add_parser("standalone", help=t("cli.project_tree.sub_standalone"))
    p.add_argument("mode", choices=("on", "off"))
    p.set_defaults(fn=cmd_standalone)
    p = sp.add_parser("claim", help=t("cli.project_tree.sub_claim"))
    p.add_argument("--root", required=True)
    p.add_argument("--sub-path", help=t("cli.project_tree.sub_path"))
    p.add_argument("--both", action="store_true", help=t("cli.project_tree.both"))
    p.set_defaults(fn=cmd_claim)
    p = sp.add_parser("pick", help=t("cli.project_tree.sub_pick"))
    p.add_argument("--sub-path")
    p.add_argument("--both", action="store_true")
    p.set_defaults(fn=cmd_pick)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
