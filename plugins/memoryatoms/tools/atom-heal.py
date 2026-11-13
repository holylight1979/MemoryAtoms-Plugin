#!/usr/bin/env python3
"""atom-heal.py — 記憶自癒（單一來源；腦內世界 P3）

分級修復一個壞掉的 atom，腳本主導、判斷才呼 LLM、修完即驗證：
  L2 broken_refs / 格式錯  → 呼 LLM 出「結構化修復提案」→ 腳本經 funnel 套用 → 驗證
  L3 stale (>60d)         → 非真壞，回 wake（不修）
  修不好（驗證不過 / needs_human）→ fixed=False + needs_human=True，由 caller 退回人工審查

單向連結不是缺陷：A → B 就代表兩顆相連，反向由索引在注入時反轉（`wg_atoms.load_link_graph`），
  不寫進 B 的檔——那是別人的檔，多人共用同一個版控庫時必衝突。

重用（不重寫）：
  tools/atom-health-check.py  偵測(single_atom_report)（importlib 載入）
  lib/atom_io.edit_metadata   改 Related（byte-stable + audit，source=tool:atom-heal）
  lib/atom_spec.validate_atom_content  格式驗證
  tools/ollama_client.get_client      本地 LLM 判斷（預設 backend；雲端為選配）

用法：
  python atom-heal.py --atom <name> [--apply] [--backend ollama|cloud|none] [--json] [--memory-root <dir>]
    無 --apply＝dry-run（只診斷分級 + L2 出提案，不寫檔）
    --memory-root 修專案層的卡（給 <專案>/.claude/memory）；不給就修全域的卡
"""
import sys, io, json, re, argparse, difflib, importlib.util
from datetime import datetime
from pathlib import Path

# Windows cp950 → 強制 UTF-8（先於 import atom-health-check，避免它二次包裝）
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent          # 程式碼根（本檔所在的樹）
TOOLS = ROOT / "tools"
HEAL_SOURCE = "tool:atom-heal"

sys.path.insert(0, str(ROOT))                          # lib.*
sys.path.insert(0, str(TOOLS))                         # ollama_client
from lib.project_root import claude_data_dir           # noqa: E402
from lib.i18n import t  # noqa: E402
MEMORY = claude_data_dir() / "memory"                  # 使用者資料根（WG_CLAUDE_DIR 或 ~/.claude）
from lib.atom_io import edit_metadata                  # noqa: E402
from lib.atom_spec import validate_atom_content        # noqa: E402
from lib.sensitive_gate import is_secret_path          # noqa: E402

# atom-health-check.py 檔名含連字號、無法直接 import → importlib 載入，重用其偵測/解析函式
_spec = importlib.util.spec_from_file_location("atom_health_check", TOOLS / "atom-health-check.py")
ahc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ahc)


# ── 共用小工具 ─────────────────────────────────────────────────────────────
def related_of(path):
    """讀該 atom 目前的 Related 清單（重用 ahc 的解析，與偵測同源）。"""
    try:
        return [r for r in ahc.parse_related(ahc.parse_frontmatter(path)) if r and r != "(none)"]
    except Exception:
        return []


def scan(name, atoms, aliases):
    """針對性重掃單一 atom（重用 single_atom_report；每次重讀檔 → 反映剛套用的修復）。"""
    return ahc.single_atom_report(name, atoms, aliases)


def classify(report, atoms):
    broken = report.get("broken_refs") or []
    stale = report.get("stale_atoms") or []
    path = atoms.get(report.get("atom"))                # 卡的檔案位置取自掃描結果（含本地根 memory/_local/）
    fmt_err = validate_atom_content(path.read_text(encoding="utf-8")) if path and path.exists() else None
    if broken or fmt_err:
        return "L2", fmt_err
    if stale:
        return "L3", None
    return "noop", None


# ── L2：死連結 / 格式 → LLM 出提案，腳本套用安全提案 ─────────────────────────
def _extract_json(s):
    """剝 ```json``` 圍欄 + 取首個 {…}（本地 crack 模型常無視 format=json 而加圍欄）。"""
    s = (s or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
    i, j = s.find("{"), s.rfind("}")
    return s[i:j + 1] if (i != -1 and j > i) else s


def _ollama_propose(name, content, bad_ref, candidates):
    """呼本地 Ollama 判斷死連結怎麼修；只回 repoint/remove/needs_human。失敗一律 needs_human。"""
    near = difflib.get_close_matches(bad_ref, candidates, n=12, cutoff=0.3) or candidates[:30]
    usr = (
        f"原子「{name}」的 Related 欄含一個指向不存在原子的死連結：「{bad_ref}」。\n"
        f"現有原子候選（可能是打錯字想指的對象）：{', '.join(near)}\n"
        "判斷該怎麼修：\n"
        f"- 若「{bad_ref}」明顯是某候選的錯字/變體 → repoint 到那個候選\n"
        "- 若該連結對象根本不該存在、無對應 → remove 移除\n"
        "- 若需要新建原子或無法判斷 → needs_human\n"
        '只回 JSON：{"action":"repoint|remove|needs_human","target":"<候選名或 null>","reason":"<簡短理由>"}'
    )
    try:
        from ollama_client import get_client
        raw = get_client().generate(usr, timeout=60, format="json")
        obj = json.loads(_extract_json(raw))
        act = obj.get("action")
        tgt = obj.get("target")
        if act not in ("repoint", "remove", "needs_human"):
            return {"action": "needs_human", "target": tgt, "reason": t("cli.atom_heal.illegal_action", action=act)}
        if act == "repoint" and tgt not in candidates:                 # 安全：repoint 必須指向真實候選
            return {"action": "needs_human", "target": tgt, "reason": t("cli.atom_heal.repoint_missing", target=tgt)}
        return {"action": act, "target": tgt, "reason": str(obj.get("reason", ""))[:200]}
    except Exception as e:
        return {"action": "needs_human", "target": None, "reason": t("cli.atom_heal.llm_failed", error=str(e)[:150])}


def _propose(name, content, bad_ref, candidates, backend):
    if backend == "ollama":
        return _ollama_propose(name, content, bad_ref, candidates)
    if backend == "cloud":
        # 選配：雲端後端（claude -p / API）。預設未實作 → 退回人工，不阻斷流程。
        return {"action": "needs_human", "target": None, "reason": t("cli.atom_heal.cloud_disabled")}
    return {"action": "needs_human", "target": None, "reason": t("cli.atom_heal.backend_disabled", backend=backend)}


def heal_L2(name, report, atoms, aliases, apply, backend, upper=None):
    """atoms＝正在修的那一層的卡；upper＝全域的卡，修專案層時才給，只當死連結改接的對象。"""
    broken = report.get("broken_refs") or []
    path = atoms.get(name)
    targets = {**(upper or {}), **atoms}
    content = path.read_text(encoding="utf-8") if path else ""
    fmt_err = validate_atom_content(content) if content else t("cli.atom_heal.atom_file_missing")
    # 別的卡不接到 secret 卡：寫進 Related 的卡名會隨 repo 外流；secret 卡自己要接什麼都行
    candidates = sorted(n for n, p in targets.items() if is_secret_path(path) or not is_secret_path(p))
    related = related_of(path) if path else []
    new_related = list(related)
    proposals, needs_human = [], False

    for b in broken:
        bad = b["missing_ref"]
        prop = _propose(name, content, bad, candidates, backend)
        proposals.append({"missing_ref": bad, **prop})
        if prop["action"] == "repoint" and prop.get("target") in targets:
            new_related = [prop["target"] if x == bad else x for x in new_related]
        elif prop["action"] == "remove":
            new_related = [x for x in new_related if x != bad]
        else:
            needs_human = True                                          # 該死連結交人工

    # 純格式問題（無死連結）：不自動改寫知識內容 → 交人工
    if fmt_err and not broken:
        needs_human = True
        proposals.append({"format_error": fmt_err, "action": "needs_human"})

    if not apply:
        return {"level": "L2", "action": "broken_ref_fix", "applied": False,
                "proposals": proposals, "fixed": False, "needs_human": needs_human, "note": "dry-run"}

    apply_res = None
    if path and new_related != related:
        res = edit_metadata(path, related=new_related, source=HEAL_SOURCE)
        apply_res = {"ok": bool(getattr(res, "ok", False)), "err": getattr(res, "error", None), "related": new_related}

    after = scan(name, atoms, aliases)
    fmt_after = validate_atom_content(path.read_text(encoding="utf-8")) if path and path.exists() else "missing"
    broken_after = after.get("broken_refs") or []
    ok = (len(broken_after) == 0) and (fmt_after is None) and not needs_human
    return {"level": "L2", "action": "broken_ref_fix", "applied": True, "proposals": proposals,
            "apply_result": apply_res,
            "verification": {"broken_after": broken_after, "format_after": fmt_after},
            "fixed": ok, "needs_human": not ok}


# ── L3：stale 喚醒（非真壞）──────────────────────────────────────────────────
def heal_L3(name, report):
    return {"level": "L3", "action": "wake", "fixed": True, "needs_human": False,
            "stale": report.get("stale_atoms") or [], "note": t("cli.atom_heal.stale_note")}


def write_heal_review(name, result, memory_root=None):
    """修不好 → 寫 _heal_review 診斷卡（JSON、非 atom 格式）+ 審計 append _merge_history.log。

    診斷卡一律放資料根的 memory/_heal_review/（/heal-review 只看這裡）；專案層的卡多記 memory_root，
    heal-review 重掃時才對得回同一個記憶目錄。"""
    review_dir = MEMORY / "_heal_review"
    review_dir.mkdir(parents=True, exist_ok=True)
    card = {"atom": name, "created_at": datetime.now().isoformat(), **result}
    if memory_root:
        card["memory_root"] = memory_root
    layer = "project" if memory_root else "global"
    (review_dir / f"{name}.json").write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    try:
        with open(MEMORY / "_merge_history.log", "a", encoding="utf-8", newline="\n") as f:
            f.write(f"{datetime.now().isoformat()}\theal_failed\t{name}\t{layer}\t{HEAL_SOURCE}\t{result.get('action', '')}\n")
    except Exception:
        pass
    return str(review_dir / f"{name}.json")


def main():
    ap = argparse.ArgumentParser(description=t("cli.atom_heal.description"))
    ap.add_argument("--atom", required=True, help=t("cli.atom_heal.atom"))
    ap.add_argument("--apply", action="store_true", help=t("cli.atom_heal.apply"))
    ap.add_argument("--auto", action="store_true", help=t("cli.atom_heal.auto"))
    ap.add_argument("--backend", default="ollama", choices=["ollama", "cloud", "none"],
                    help=t("cli.atom_heal.backend"))
    ap.add_argument("--json", action="store_true", help=t("cli.atom_heal.json"))
    ap.add_argument("--memory-root", default=None, help=t("cli.atom_heal.memory_root"))
    a = ap.parse_args()

    memory_root, upper = None, None                    # 預設修全域的卡
    if a.memory_root:
        root = Path(a.memory_root)
        if not root.is_dir():
            print(t("cli.atom_heal.memory_root_missing", path=a.memory_root), file=sys.stderr)
            sys.exit(1)
        if ahc.use_memory_root(root):                  # 專案層：往全域卡的引用照樣算有效，不會被當死連結拿去修
            memory_root = str(root.resolve())
            upper = ahc.find_atoms(ahc.GLOBAL_MEMORY_ROOT)   # 全域的卡也是改接候選；重掃只掃這一層
    atoms = ahc.find_atoms(ahc.MEMORY_ROOT)
    aliases = ahc.parse_memory_index(ahc.MEMORY_ROOT)
    report = scan(a.atom, atoms, aliases)

    if not report.get("exists"):
        out = {"atom": a.atom, "level": "missing", "action": "none",
               "fixed": False, "needs_human": True, "reason": t("cli.atom_heal.atom_not_found")}
    else:
        level, fmt_err = classify(report, atoms)
        if level == "L2" and a.auto:
            out = {"level": "L2", "action": "needs_manual", "fixed": False, "needs_human": False,
                   "note": t("cli.atom_heal.l2_manual_note"), "broken_refs": report.get("broken_refs")}
        elif level == "L2":
            out = heal_L2(a.atom, report, atoms, aliases, a.apply, a.backend, upper)
        elif level == "L3":
            out = heal_L3(a.atom, report)
        else:
            out = {"level": "noop", "action": "none", "fixed": True,
                   "needs_human": False, "note": t("cli.atom_heal.healthy_note")}
        out["atom"] = a.atom
        out["backend"] = a.backend
        # 修不好（驗證不過）→ 寫 _heal_review 退人工審查（僅 L2 實修失敗）
        if a.apply and out.get("needs_human") and out.get("level") == "L2":
            out["heal_review_card"] = write_heal_review(a.atom, out, memory_root)

    out["health_before"] = {
        "broken": len(report.get("broken_refs") or []),
        "stale": len(report.get("stale_atoms") or []),
    }
    print(json.dumps(out, ensure_ascii=False, **({} if a.json else {"indent": 2})))


if __name__ == "__main__":
    main()
