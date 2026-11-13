#!/usr/bin/env python3
"""heal-review.py — 記憶自癒失敗佇列的人工裁決後端（/heal-review skill）

atom-heal.py 修不好（驗證不過 / needs_human）時會在 memory/_heal_review/<atom>.json
留診斷卡。本工具列卡 + 人工裁決：
  list                列出待人工的卡（atom / 壞在哪 / LLM 提案 / 失敗原因）
  show  <atom>        看單張卡完整內容
  resolve <atom>      標記已修好 → 清卡（會先重掃確認真的健康；仍有問題、或重掃失敗沒能確認，都要 --force 才清）
  dismiss <atom>      決定不修 → 清卡（won't-fix）
專案層的卡（診斷卡有 memory_root）回同一個記憶目錄重掃，紀錄列的層別寫 project。
resolve/dismiss 的裁決資格看 workflow/config.json review.deciders（空＝人人可裁決；與 conflict-review 同源）。
JSON over stdout；非零 exit code 代表操作失敗。
"""
import sys, io, json, argparse, subprocess
from datetime import datetime, timezone
from pathlib import Path

if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

CODE_DIR = Path(__file__).resolve().parent.parent     # 程式碼根（本檔所在的樹）
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))
from lib.project_root import claude_data_dir          # noqa: E402
from lib.wg_platform import no_window_flags  # noqa: E402
from lib.i18n import t  # noqa: E402
CLAUDE_DIR = claude_data_dir()                        # 使用者資料根（WG_CLAUDE_DIR 或 ~/.claude）
MEMORY = CLAUDE_DIR / "memory"
REVIEW = MEMORY / "_heal_review"
TOOLS = CODE_DIR / "tools"

sys.path.insert(0, str(CODE_DIR / "hooks"))
try:
    from wg_roles import is_management, get_current_user   # noqa: E402
except Exception:
    def is_management(): return True          # 角色模組缺 → 不阻擋（單人環境）
    def get_current_user(): return "unknown"


def _iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(action, atom, by, detail, layer="global"):
    try:
        with open(MEMORY / "_merge_history.log", "a", encoding="utf-8", newline="\n") as f:
            f.write("\t".join([_iso(), action, atom, layer, by or "-", detail or "-"]) + "\n")
    except OSError:
        pass


def list_cards():
    cards = []
    if REVIEW.is_dir():
        for f in sorted(REVIEW.glob("*.json")):
            try:
                cards.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                cards.append({"atom": f.stem, "_parse_error": True})
    return cards


def rescan(atom, memory_root=None):
    """重掃一張卡，回 (剩幾個問題, 健檢結果)。
    沒掃成回 (-1, {"_err": 原因})：記的記憶目錄在這台不存在、健檢跑不動或輸出讀不懂、掃不到這張卡。
    沒掃成不等於零問題，resolve 不能拿它當健康放行。"""
    cmd = [sys.executable, str(TOOLS / "atom-health-check.py"), "--atom", atom, "--json"]
    if memory_root:          # 專案層的卡要對回同一個記憶目錄重掃；掃全域會找不到這張卡
        if not Path(memory_root).is_dir():     # 診斷卡會隨背景同步到別台，記的是原本那台的路徑
            return -1, {"_err": t("cli.heal_review.memory_root_gone", path=memory_root)}
        cmd += ["--memory-root", memory_root]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60,
                           creationflags=no_window_flags())
    except (OSError, subprocess.SubprocessError) as e:     # 起不來、逾時
        return -1, {"_err": t("cli.heal_review.rescan_error", detail=str(e)[:300])}
    try:
        h = json.loads(r.stdout)
        n = len(h["broken_refs"]) + len(h["missing_reverse_refs"])
    except (ValueError, TypeError, KeyError) as e:         # 輸出讀不懂：多半是健檢自己報錯退出，原因在 stderr
        detail = (r.stderr or "").strip()[-300:] or str(e)[:300]
        return -1, {"_err": t("cli.heal_review.rescan_error", detail=detail)}
    if not h.get("exists"):
        return -1, {**h, "_err": t("cli.heal_review.atom_not_rescanned", root=memory_root or str(MEMORY))}
    return n, h


def emit(obj, as_json, code=0):
    print(json.dumps(obj, ensure_ascii=False, **({} if as_json else {"indent": 2})))
    sys.exit(code)


def main():
    ap = argparse.ArgumentParser(description=t("cli.heal_review.description"))
    ap.add_argument("action", choices=["list", "show", "resolve", "dismiss"])
    ap.add_argument("atom", nargs="?")
    ap.add_argument("--force", action="store_true", help=t("cli.heal_review.force"))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.action == "list":
        cards = list_cards()
        summary = [{
            "atom": c.get("atom"), "level": c.get("level"), "action": c.get("action"),
            "needs_human": c.get("needs_human"), "created_at": c.get("created_at"),
            "broken_after": (c.get("verification") or {}).get("broken_after"),
            "proposals": c.get("proposals"), "memory_root": c.get("memory_root"),
        } for c in cards]
        emit({"ok": True, "count": len(summary), "cards": summary}, a.json)

    if not a.atom:
        emit({"ok": False, "error": t("cli.heal_review.atom_required")}, a.json, 1)
    p = REVIEW / f"{a.atom}.json"

    if a.action == "show":
        if not p.exists():
            emit({"ok": False, "error": t("cli.heal_review.card_not_found")}, a.json, 1)
        emit({"ok": True, "card": json.loads(p.read_text(encoding="utf-8"))}, a.json)

    # resolve / dismiss → 需 management
    if not is_management():
        emit({"ok": False, "error": t("cli.heal_review.need_management")}, a.json, 1)
    if not p.exists():
        emit({"ok": False, "error": t("cli.heal_review.card_not_found")}, a.json, 1)
    by = get_current_user()
    try:
        card = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        card = None                    # 卡壞了讀不懂：照全域重掃；掃不到這張卡就不放行（rescan 回 -1）
    memory_root = card.get("memory_root") if isinstance(card, dict) else None
    layer = "project" if memory_root else "global"

    if a.action == "resolve":
        n, h = rescan(a.atom, memory_root)
        if n < 0 and not a.force:      # 沒掃成＝沒確認過，不當零問題清卡
            emit({"ok": False, "error": t("cli.heal_review.rescan_failed", reason=h.get("_err", "")), "health": h}, a.json, 1)
        if n > 0 and not a.force:
            emit({"ok": False, "error": t("cli.heal_review.still_broken", count=n), "health": h}, a.json, 1)
        p.unlink()
        _log("heal_resolved", a.atom, by, f"remaining={n}", layer)
        out = {"ok": True, "resolved": a.atom, "remaining_issues": n}
        if n < 0:
            out["rescan_error"] = h.get("_err")     # --force 清掉的：留下沒掃成的原因
        emit(out, a.json)

    if a.action == "dismiss":
        p.unlink()
        _log("heal_dismissed", a.atom, by, "won't-fix", layer)
        emit({"ok": True, "dismissed": a.atom}, a.json)


if __name__ == "__main__":
    main()
