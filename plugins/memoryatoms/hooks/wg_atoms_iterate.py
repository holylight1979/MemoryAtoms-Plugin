"""
wg_atoms_iterate.py — 卡片自我迭代：封存分數、staging 目錄、依實際使用統計晉升或封存 atom（非自評）。

誰叫它：SessionEnd 經 wg_atoms 呼叫。
純搬移自 wg_atoms.py，函式本體未改；hooks/wg_atoms.py 仍 re-export 這裡的名稱。
"""

import math
import re
import sys
from typing import Any, Dict, Optional
from wg_core import (
    CLAUDE_DIR,
    MEMORY_DIR,
    _atom_debug_error,
    log_promotion_audit,
    log_promotion_heartbeat,
)
from pathlib import Path
from datetime import datetime
import i18n   # lib/：給人看的提示走語言表
from wg_atoms_compat import iter_atom_files_multi
from wg_atoms_forget import apply_selective_forget
from wg_atoms_realm import _autocapture_unconfirmed_from_text


# ─── Self-Iteration: atom 晉升 (was wg_iteration._self_iterate_atoms) ────────


def _staging_dir_for_atom(md_file: Path) -> Path:
    """候選 atom 所屬記憶庫的 _staging/（不看 cwd）。

    專案庫（<root>/.claude/memory/ 或舊址 ~/.claude/projects/<slug>/memory/）→ 該庫 _staging；
    其餘（~/.claude/memory/ 含 memory/_local/、舊址 _AIDocs/_atoms/、_AIDocs/Failures/）→ 全域 memory/_staging。
    """
    projects_dir = CLAUDE_DIR / "projects"
    for p in md_file.parents:
        if p.name != "memory":
            continue
        if p == MEMORY_DIR:
            break
        if p.parent.name == ".claude" or p.parent.parent == projects_dir:
            return p / "_staging"
    return MEMORY_DIR / "_staging"


def archive_score(acc: Dict[str, Any], today: datetime, decay_half_life: float,
                  *, has_use_evidence: bool = False) -> Optional[Dict[str, Any]]:
    """封存分數（selective forget 的唯一公式；memory-audit 也用這個）：
    score = 0.5·recency（半衰期 decay_half_life 天）+ 0.5·usage（log10(max(conf, hits)+1)/2）。
    無任何活動訊號（沒 last_used、或 conf/hits/效用全 0）→ None（不評、不封存）。"""
    last_used_raw = acc.get("last_used")
    confirmations = int(acc.get("confirmations") or 0)
    readhits = int(acc.get("read_hits") or 0)
    if not last_used_raw or (confirmations == 0 and readhits == 0 and not has_use_evidence):
        return None
    try:
        last_used = datetime.strptime(last_used_raw, "%Y-%m-%d")
    except ValueError:
        return None
    days_since = (today - last_used).days
    recency = math.exp(-math.log(2) * max(days_since, 0) / decay_half_life)
    usage = min(1.0, math.log10(max(confirmations, readhits) + 1) / 2)
    return {"score": 0.5 * recency + 0.5 * usage, "days_since": days_since,
            "last_used": last_used, "confirmations": confirmations, "readhits": readhits}


def _self_iterate_atoms(
    state: Dict[str, Any], config: Dict[str, Any]
) -> Dict[str, Any]:
    """Atom decay scoring + 效用驅動 [臨]→[觀] auto-promotion.

    Runs at SessionEnd. Scans all atom files, calculates health scores,
    auto-promotes [臨] items in mature atoms, reports archive/demote candidates.

    效用驅動：
      - 慢衰減：每顆 atom α←1+λ(α−1); β←1+λ(β−1)（λ≈0.97），把效用證據往 prior 拉。
      - 晉升只看效用 Wilson 下界；Confirmations 與 ReadHits 都不參與晉升。
      - Wilson 下界 ≤ demote_lb 且 n≥min_n → 列降級候選（不自動降，留裁決）。
    """
    si_config = config.get("self_iteration", {})
    u_config = config.get("usefulness", {}) or {}
    decay_half_life = si_config.get("decay_half_life_days", 30)
    archive_threshold = si_config.get("archive_score_threshold", 0.3)
    # 效用旋鈕（py↔js 鏡像：server.js）
    decay_lambda = float(u_config.get("decay_lambda", 0.97))
    promote_lb = float(u_config.get("promote_lb", 0.6))
    demote_lb = float(u_config.get("demote_lb", 0.35))
    min_n = int(u_config.get("min_n", 3))
    # demote 側門檻較嚴（n≥5）：67-75% 成功率的 atom 不因小樣本波動列降級候選
    demote_min_n = int(u_config.get("demote_min_n", 5))
    try:
        from lib.atom_access import WILSON_Z_DEFAULT
        wilson_z: Optional[float] = float(u_config.get("wilson_z", WILSON_Z_DEFAULT))
    except ImportError:
        wilson_z = None   # lib 載不到：下面每顆的效用統計也會停用（usefulness_stats=None），用不到 z

    results = {"promoted": [], "archive_candidates": [],
               "demote_candidates": [], "scanned": 0}
    today = datetime.now()

    # 明確「有用/誤導」票：先把別機／別人的票套進本機 sidecar，再進掃描（fail-open）
    try:
        from lib.atom_feedback import fold_votes
        _cwd = ((state or {}).get("session") or {}).get("cwd") or None
        results["votes_folded"] = fold_votes(cwd=Path(_cwd) if _cwd else None)
    except Exception as e:  # noqa: BLE001 — 輔助步驟不得中斷 SessionEnd
        print(f"[atom_feedback] fold failed: {e}", file=sys.stderr)

    # V5+: 全域 atom 搜尋（memory + _AIDocs/Failures/）統一委派 lib.atom_locations。
    # 判定走 is_atom_file（同 MEMORY.md/_*/SPEC_* skip）+ failures stems 過濾參考文件。
    if iter_atom_files_multi is not None:
        md_files_iter = iter_atom_files_multi()
    else:
        md_files_iter = (m for m in MEMORY_DIR.glob("*.md")
                         if m.name not in ("MEMORY.md", "SPEC_Atomic_Memory_System.md")
                         and not m.name.startswith("_"))

    for md_file in md_files_iter:
        try:
            text = md_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        results["scanned"] += 1

        try:
            from lib.atom_access import (
                read_access, decay_usefulness,
                usefulness_stats, usefulness_promote_eligible,
                usefulness_demote_candidate,
            )
            acc = read_access(md_file)
            # Step 6 慢衰減（SessionEnd）：把 (α,β) 往 prior 拉，回填衰減後值供晉升判定
            try:
                na, nb = decay_usefulness(
                    md_file, lam=decay_lambda, source="hook:atom-decay")
                acc["useful_hits"], acc["used_fail"] = na, nb
            except (OSError, ValueError):
                pass
        except (ImportError, OSError):
            acc = {}
            usefulness_stats = None  # type: ignore
            usefulness_promote_eligible = None  # type: ignore
            usefulness_demote_candidate = None  # type: ignore
        u_stats = usefulness_stats(acc, z=wilson_z) if usefulness_stats else {"n": 0}
        has_use_evidence = u_stats.get("n", 0) > 0

        sc = archive_score(acc, today, decay_half_life, has_use_evidence=has_use_evidence)
        if sc is None:
            continue  # 無任何活動訊號（注入/確認/效用）→ 跳過
        last_used_raw = acc.get("last_used")
        confirmations, readhits = sc["confirmations"], sc["readhits"]
        last_used, days_since, score = sc["last_used"], sc["days_since"], sc["score"]

        if score < archive_threshold:
            results["archive_candidates"].append({
                "atom": md_file.stem,
                "path": str(md_file),
                "score": round(score, 3),
                "last_used": last_used_raw,
                "confirmations": confirmations,
            })

        # 晉升只看效用 Wilson 下界（升≥promote_lb 且 n≥min_n）；Confirmations、ReadHits 都不參與。
        # py↔js 鏡像：server.js toolAtomPromote usefulness gate。
        util_eligible = bool(
            usefulness_promote_eligible
            and usefulness_promote_eligible(
                acc, promote_lb=promote_lb, min_n=min_n, z=wilson_z)
        )
        promote_method = "usefulness"
        # INV-PROMOTION-GATE-ON-SCAN-FACE：未確認 auto-capture 碎片不得自動晉升（與 realm sweep
        # 路徑 _sweep_realm_auto_migrate:1768 同源規則：碎片是未經人確認的 [臨] 萃取，confirmations
        # 達標也不算數，待人工確認/晉升後才算）。斷『佔位符碎片被當穩定 atom 自動晉升』漏洞——
        # 該過濾原僅在 realm sweep，晉升掃描面缺，故碎片曾被算晉升。手動 atom_promote（js）為
        # 人工確認路徑、不受此限。
        if util_eligible and not _autocapture_unconfirmed_from_text(text):
            lines = text.split("\n")
            promoted_in_file = []
            changed = False
            for i, line in enumerate(lines):
                if re.match(r"^- \[臨\]", line):
                    lines[i] = line.replace("- [臨]", "- [觀]", 1)
                    desc = line.split("[臨]", 1)[-1].strip()[:60]
                    promoted_in_file.append(desc)
                    changed = True

            if changed:
                prefixes = set()
                for L in lines:
                    pm = re.match(r"^- \[([臨觀固])\]", L)
                    if pm:
                        prefixes.add(pm.group(1))
                header_promoted = False
                if prefixes == {"觀"}:
                    for i, line in enumerate(lines):
                        hm = re.match(r"^(- Confidence:\s*)\[臨\]\s*$", line)
                        if hm:
                            lines[i] = f"{hm.group(1)}[觀]"
                            header_promoted = True
                            break

                tmp = md_file.with_suffix(".tmp")
                try:
                    tmp.write_text("\n".join(lines), encoding="utf-8", newline="\n")
                    tmp.replace(md_file)
                except OSError:
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
                results["promoted"].append({
                    "atom": md_file.stem,
                    # 實體路徑：SessionEnd 的自動提交要按檔名清單選擇性 stage，
                    # 只有 stem 無法定位（atom 散在 memory/ 與 _AIDocs/ 多根）。
                    "path": str(md_file),
                    "items": promoted_in_file,
                    "confirmations": confirmations,
                    "method": promote_method,
                    "lower_bound": round(u_stats.get("lower_bound", 0.0), 3),
                })
                log_promotion_audit(
                    "auto_observe", md_file.stem,
                    items=len(promoted_in_file),
                    confirmations=confirmations,
                    header_promoted=header_promoted,
                    method=promote_method,
                    lower_bound=round(u_stats.get("lower_bound", 0.0), 3),
                )

        # 效用 Wilson 下界 ≤ demote_lb 且 n≥demote_min_n、且仍有非[臨]條目 → 降級候選
        # （不自動降，屬敏感裁決；列入 staging 報告供管理職審視）。
        if (usefulness_demote_candidate
                and usefulness_demote_candidate(
                    acc, demote_lb=demote_lb, min_n=demote_min_n, z=wilson_z)
                and re.search(r"^- \[(觀|固)\]", text, re.MULTILINE)):
            results["demote_candidates"].append({
                "atom": md_file.stem,
                "path": str(md_file),
                "lower_bound": round(u_stats.get("lower_bound", 0.0), 3),
                "alpha": u_stats.get("alpha"),
                "beta": u_stats.get("beta"),
                "n": u_stats.get("n"),
            })

    # 報告落「候選 atom 所屬記憶庫」的 _staging/（全域 → ~/.claude/memory/_staging；專案 →
    # 專案 _staging），不看 cwd——否則專案 session 會把全域候選寫進專案庫。分庫時各寫一份。
    groups: Dict[Path, Dict[str, list]] = {}
    for kind in ("archive_candidates", "demote_candidates"):
        for c in results[kind]:
            g = groups.setdefault(_staging_dir_for_atom(Path(c["path"])),
                                  {"archive_candidates": [], "demote_candidates": []})
            g[kind].append(c)
    results["reports"] = []
    forget_all = {"mode": "dry_run", "candidates": [], "forgotten": [], "skipped": [],
                  "moved": [], "index_errors": [], "catalog_errors": []}
    for staging, g in groups.items():
        staging.mkdir(parents=True, exist_ok=True)
        out_lines = [
            f"# Archive / Demote Candidates ({today.strftime('%Y-%m-%d')})\n",
        ]
        if g["archive_candidates"]:
            out_lines.append(i18n.t("hook.wg_atoms_iterate.archive_heading", threshold=archive_threshold))
            for c in g["archive_candidates"]:
                out_lines.append(
                    f"- **{c['atom']}** — score={c['score']}, "
                    f"last_used={c['last_used']}, confirmations={c['confirmations']}"
                )
        if g["demote_candidates"]:
            out_lines.append(i18n.t("hook.wg_atoms_iterate.demote_heading", lb=demote_lb, min_n=demote_min_n))
            for c in g["demote_candidates"]:
                out_lines.append(
                    f"- **{c['atom']}** — lower_bound={c['lower_bound']}, "
                    f"α={c['alpha']}, β={c['beta']}, n={c['n']}"
                )
        report = staging / "archive-candidates.md"
        report.write_text("\n".join(out_lines), encoding="utf-8", newline="\n")
        results["reports"].append(str(report))

        # Phase D — selective forgetting（預設 dry-run：只寫候選；enabled+!dry_run 才隔離 _distant/）
        try:
            fr = apply_selective_forget(
                g["archive_candidates"], config,
                atoms_dir=staging.parent, staging_dir=staging)
            for k in ("candidates", "forgotten", "skipped", "moved", "index_errors"):
                forget_all[k] += fr[k]
            if fr["mode"] == "isolated":
                forget_all["mode"] = "isolated"
            if fr.get("catalog_error"):
                forget_all["catalog_errors"].append(f"{staging.parent}: {fr['catalog_error']}")
        except Exception as e:
            _atom_debug_error("forget:apply", e)
    if groups:
        results["forget"] = forget_all

    # 無晉升事件的掃描也要留活性證據，週健檢才能分辨「無事件」與「管線停擺」
    if not results["promoted"]:
        log_promotion_heartbeat(scanned=results["scanned"])

    return results
