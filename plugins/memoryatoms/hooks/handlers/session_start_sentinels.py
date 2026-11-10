"""
handlers/session_start_sentinels.py — SessionStart 的「哨兵與死人開關」

做什麼：開場時檢查幾件「壞了沒人會發現」的事，有異常才回提示行（無事零 context）：
IDENTITY.md 與必載檔硬契約有沒有被改壞、上個 SessionEnd 有沒有沒跑完、週健檢排程有沒有死、回訪到期項。
誰叫它：handlers/session_start.py 的 handle_session_start（依序呼叫，結果併進開場提示）。
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from wg_core import CODE_DIR, WORKFLOW_DIR, _atom_debug_error
from wg_platform import no_window_flags
from edition import layered_meta   # lib/：插件版出廠詞表墊底、資料根有同名檔才蓋過
import i18n   # lib/：給人看的提示走語言表


def check_identity_integrity(data_dir: Path) -> List[str]:
    """IDENTITY.md 完整性哨兵，由硬契約登記表驅動：登記表裡 live 為 IDENTITY.md 的那一筆決定查什麼。

    該筆的 `sentinel`（沒寫取 must_contain 第一句）要在檔內、檔案大小要 ≥ `min_bytes`（預設 2000）；缺 → 告警行。
    資料根沒有 IDENTITY.md、沒有登記表、或登記表沒列 IDENTITY.md → 不查（沒用個人化啟動，不算降級）。"""
    identity = data_dir / "IDENTITY.md"
    if not identity.exists():
        return []
    reg_path = layered_meta("always-load-contracts.json", data_dir / "memory")
    try:
        contracts = json.loads(reg_path.read_text(encoding="utf-8")).get("contracts") or []
    except (OSError, ValueError):
        return []   # 登記表缺／壞：缺由「沒登記就不查」處理，壞由 check_always_load_contracts 回報
    entry = next((c for c in contracts if isinstance(c, dict) and c.get("live") == "IDENTITY.md"), None)
    if entry is None:
        return []
    must = entry.get("must_contain") or []
    sentinel = entry.get("sentinel") or (must[0] if must else "")
    min_bytes = entry.get("min_bytes") if isinstance(entry.get("min_bytes"), int) else 2000
    size = identity.stat().st_size
    text = identity.read_text(encoding="utf-8", errors="ignore")
    if (not sentinel or sentinel in text) and size >= min_bytes:
        return []
    return [i18n.t("hook.session_start_sentinels.identity_damaged", path=identity, size=size)]


def check_always_load_contracts(data_dir: Path) -> List[str]:
    """必載檔硬契約哨兵：資料根 memory/_meta/always-load-contracts.json 登記的句子在 live 檔缺席 → 告警行。

    契約句被修剪／覆寫時，模型當 session 就失去事前依據（事後閘只看狀態不懂語意）。
    資料根沒有登記表、或沒有那份 live 檔（IDENTITY／USER／rules）→ 不查，不算降級；登記表壞了 → 回一行告警（不阻斷）。
    """
    reg_path = layered_meta("always-load-contracts.json", data_dir / "memory")
    if not reg_path.exists():
        return []
    try:
        reg = json.loads(reg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return [i18n.t("hook.session_start_sentinels.contract_registry_unreadable", name=reg_path.name, err=e)]
    out: List[str] = []
    for c in reg.get("contracts") or []:
        live = data_dir / str(c.get("live", ""))
        if not live.is_file():
            continue
        try:
            text = live.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        missing = [m for m in (c.get("must_contain") or []) if m not in text]
        if missing:
            out.append(i18n.t("hook.session_start_sentinels.contract_missing", live=c.get('live'), id=c.get('id'),
                              missing=i18n.t("hook.session_start_sentinels.contract_list_sep").join(missing),
                              fix=c.get('fix', i18n.t("hook.session_start_sentinels.contract_fix_default"))))
    return out


def _check_se_sentinel_residual(lines: List[str], min_age_s: float = 60.0) -> None:
    """SessionEnd 哨兵殘留（session_end._se_sentinel_arm 留下、未被正常收尾拆除）
    → 告警一行 + 清除。只認 mtime 超過 min_age_s 者：並行 session 的 SessionEnd
    可能正在跑（<30s 窗），剛 arm 的哨兵不是殘留，不得誤清誤報。"""
    import time as _time
    se_dir = wg_core_workflow_dir() / "se-sentinel"
    if not se_dir.is_dir():
        return
    now = _time.time()
    residual = []
    for p in sorted(se_dir.glob("*.json")):
        try:
            if now - p.stat().st_mtime > min_age_s:
                residual.append(p)
        except OSError:
            continue
    if not residual:
        return
    sids = ", ".join(p.stem[:12] + "…" for p in residual[:3])
    lines.append(i18n.t("hook.session_start_sentinels.se_sentinel_residual", n=len(residual), sids=sids))
    for p in residual:
        try:
            p.unlink()
        except OSError:
            pass


def wg_core_workflow_dir() -> Path:
    """取 wg_core.WORKFLOW_DIR 的即時值（測試 monkeypatch wg_core 後仍生效）。"""
    import wg_core
    return wg_core.WORKFLOW_DIR


HEALTH_RUN_STALE_DAYS = 10  # 週排程 + 3 天寬限；超過 = 排程器本身死了


def _health_advisory(last_run_path) -> list:
    """週健檢死人開關 → advisory 行（無異常回 []，不佔 context）。

    三種浮出（last-run 缺檔＝還沒開始用，不提）：at 逾 HEALTH_RUN_STALE_DAYS 天
    （排程停擺）、上次健檢 red>0（有待處理項未看）、last-run 不可解析。自身壞掉走
    _atom_debug_error，不阻斷 SessionStart。
    """
    try:
        if not last_run_path.exists():
            return []   # 還沒開始用週健檢不算異常；真的逾期才提（見下）
        d = json.loads(last_run_path.read_text(encoding="utf-8"))
        at = datetime.fromisoformat(d.get("at", ""))
        age = (datetime.now() - at).days
        out = []
        if age > HEALTH_RUN_STALE_DAYS:
            out.append(i18n.t("hook.session_start.health_stale", age=age, last=f"{at:%Y-%m-%d}",
                              script=(CODE_DIR / "tools" / "health-weekly.py").as_posix()))
        if int(d.get("red", 0)) > 0:
            out.append(i18n.t("hook.session_start_sentinels.health_red", red=d['red'], report=d.get('report', 'workflow/health-reports/')))
        return out
    except Exception as e:
        _atom_debug_error("session_start:health_advisory", e)
        return [i18n.t("hook.session_start.health_unparseable", script=(CODE_DIR / "tools" / "health-weekly.py").as_posix())]


def _followup_advisory() -> list:
    """回訪到期 → 開場自動跑 tools/followup-check.py，把檢查結果＋自足交接推進 context。

    存在理由：「一週後再看數據」在 session 關掉後必然被遺忘；登記表 workflow/followups.json
    以「接手者零記憶」寫 handoff，到期後使用者任何一次開 CC 都會看到並能直接行動（登記項標了 owner／host 的，只有那個人、那台機器看得到）。
    每日提醒一次（--mark-shown），PASS 自動結案（--auto-close），首次整份、之後精簡（--brief）。
    純子程序、fail-open：失敗只 debug log，不阻斷 SessionStart。無到期項回 []。
    """
    try:
        import subprocess
        reg = WORKFLOW_DIR / "followups.json"
        if not reg.exists():
            return []
        r = subprocess.run(
            [sys.executable, str(CODE_DIR / "tools" / "followup-check.py"),
             "--run", "--auto-close", "--brief", "--mark-shown"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25,
            creationflags=no_window_flags())
        out = (r.stdout or "").strip()
        return [out] if out else []
    except Exception as e:
        _atom_debug_error("session_start:followup_advisory", e)
        return [i18n.t("hook.session_start_sentinels.followup_failed")]
