"""
handlers/session_start_vector.py — SessionStart 的向量服務旗標與提示

做什麼：開場時 ping 向量服務，服務活著就立好 vector_ready.flag、死了就拆掉；
服務活著但索引重建失敗／沒有嵌入後端／是別的資料根的服務，或服務沒起且上次啟動失敗 → 開場提示或紅字。
誰叫它：handlers/session_start.py 的 handle_session_start（健檢段與收尾段）。
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from wg_core import CLAUDE_DIR, WORKFLOW_DIR, degraded_notify, vector_service_port
import i18n   # lib/：給人看的提示走語言表
import vector_venv   # lib/：向量套件裝在哪、裝了沒、插件版還是安裝版

RECALL_WITH_VECTOR = "71.4%"      # 換句話問的路標送達率（BRIEF 第 3 節 M 實測）：有向量
RECALL_WITHOUT_VECTOR = "51.8%"   # 同一組題、只剩 trigger＋BM25


def _invite_on(config: Dict[str, Any]) -> bool:
    """插件版的「請說 /memoryatoms:setup-vector」紅字開著沒：vector_search.invite=false 就不印（使用者不想裝）。"""
    return (config.get("vector_search") or {}).get("invite", True) is not False


def _vector_package_notice(config: Dict[str, Any]) -> None:
    """開場：向量套件 lancedb 沒裝 → 紅字。安裝版照舊叫人 pip install；插件版改成邀請說 /memoryatoms:setup-vector
    （套件裝進插件專用 venv，不動系統 Python），附有／沒有向量的送達率；invite=false 時不印。設定關掉向量也不印。"""
    if not config.get("vector_search", {}).get("enabled", True) or vector_venv.lancedb_available():
        return
    if vector_venv.plugin_edition():
        if _invite_on(config):
            degraded_notify("vector_invite", command=vector_venv.SETUP_COMMAND,
                            with_vector=RECALL_WITH_VECTOR, without_vector=RECALL_WITHOUT_VECTOR)
        return
    degraded_notify("vector_package", package="lancedb", python=re.sub(r"pythonw(?=(\.exe)?$)", "python", sys.executable))


def _refresh_vector_flag(
    config: Dict[str, Any], *, flag_path: Optional[Path] = None
) -> str:
    """SessionStart 冷啟動關窗：服務已暖（health 200）則寫/保留 `vector_ready.flag`，
    首個 prompt 即可用 vector；ping 失敗才拆 flag（fail-closed，防信任指向死服務的舊
    flag——27d 靜默失效的教訓）。回 'kept'（服務活）/ 'cleared'（無回應→拆）。

    下方 fire-and-forget bg subprocess 仍會重啟服務 + 重驗/重建 flag + probe log；
    此僅在服務本就常駐時提前把 flag 立好，消掉「拆→async 重建」之間的 no_flag 空窗
    （該空窗內早期搜尋 fallback 到 keyword）。ping timeout 短：服務活 ~ms、死則
    connection-refused 立即失敗，故對 SessionStart 延遲影響可忽略。
    """
    flag = flag_path or (WORKFLOW_DIR / "vector_ready.flag")
    port = vector_service_port(config)
    try:
        import urllib.request
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1.0)
    except Exception:
        try:
            flag.unlink(missing_ok=True)
        except OSError:
            pass
        return "cleared"
    try:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text("ready", encoding="utf-8", newline="\n")
    except OSError:
        pass
    return "kept"


def _service_json(port: int, path: str) -> Optional[Dict[str, Any]]:
    """GET 向量服務的一個端點（1 秒逾時）；連不上、逾時、回的不是 JSON 物件都回 None。"""
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=1.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _vector_index_advisory(config: Dict[str, Any]) -> list:
    """向量服務活著、但上次索引重建失敗 → 一行 advisory；其餘回 []。

    服務活著時 statusline 照亮 vec✓，重建失敗只寫在 /status 的 index_job.error，
    不浮出就是新卡與搬過的卡長期搜不到卻沒人知道。服務沒回應不在這裡報（_notify_vector_down）。
    順帶：服務活著但沒有嵌入後端（Ollama 連不到、備援也不可用），或這個埠上是別的資料根的服務 → 走開場紅字。
    這兩件先問 /health（只讀記憶體，毫秒回）：/status 要開 LanceDB，剛起的服務第一次問要先載 lancedb（實測 4 秒），
    超過 1 秒逾時，整段被吞、紅字就不出（全新安裝重開後的第一場開場就是這樣）。/health 已說沒有嵌入後端就不問 /status：
    索引本來就建不起來，重建失敗那行只是同一件事再講一次。舊版服務的 /health 沒帶 data_root → 照舊從 /status 拿。
    """
    port = vector_service_port(config)
    health = _service_json(port, "/health")
    if health is None:
        return []
    no_embedder = health.get("embedder") == "none" and "data_root" in health
    status = {} if no_embedder else (_service_json(port, "/status") or {})
    service = {**(status.get("service") or {}), **health}   # /health 有的以它為準：不會被冷啟動的 /status 拖住
    if service.get("embedder") == "none":
        model = (config.get("vector_search") or {}).get("embedding_model") or "qwen3-embedding"   # 紅字帶設定裡的模型名；沒設用出廠值
        if not vector_venv.plugin_edition():
            degraded_notify("vector_embedder", log=CLAUDE_DIR / "Logs" / "vector-service.log", model=model)
        elif _invite_on(config):   # 插件版：Ollama 或模型還沒裝，指回 setup-vector 的後兩步
            degraded_notify("vector_embedder_invite", command=vector_venv.SETUP_COMMAND, model=model,
                            with_vector=RECALL_WITH_VECTOR, without_vector=RECALL_WITHOUT_VECTOR)
    served_root = service.get("data_root")
    if served_root and os.path.normcase(os.path.normpath(served_root)) != os.path.normcase(os.path.normpath(str(CLAUDE_DIR))):
        degraded_notify("vector_foreign", port=port, root=served_root)   # 兩套安裝同埠：抄來的 service_port，或雜湊撞埠
    job = status.get("index_job") or {}
    err = job.get("error")
    if not err:
        return []
    return [i18n.t("hook.session_start_vector.rebuild_failed", err=str(err)[:120])]


VECTOR_SPAWN_LOCK_TTL_S = 150.0   # 同 tools/memory-vector-service/starter.py SPAWN_LOCK_TTL_S：這段時間內有人正在起服務


def _notify_vector_down(config: Dict[str, Any]) -> None:
    """服務沒回應時才問（_refresh_vector_flag 回 cleared）：上一次啟動已跑完而且失敗、現在也沒人正在起 → 開場紅字。
    從沒起過、或正在自動啟動都不算缺。使用者自己關掉向量（vector_search.enabled=false）也不算。"""
    if not config.get("vector_search", {}).get("enabled", True):
        return
    if vector_venv.plugin_edition() and not vector_venv.lancedb_available():
        return   # 插件版還沒裝套件：starter 不起服務，邀請那行已經講了
    try:
        tail = (CLAUDE_DIR / "Logs" / "vector-observation-probe.log").read_text(encoding="utf-8", errors="ignore").splitlines()
        if not tail or json.loads(tail[-1]).get("flag_state") != "no_flag":
            return
    except (OSError, ValueError, AttributeError):
        return
    try:
        if time.time() - (WORKFLOW_DIR / "vector_starting.lock").stat().st_mtime < VECTOR_SPAWN_LOCK_TTL_S:
            return
    except OSError:
        pass
    degraded_notify("vector_service", port=vector_service_port(config), log=CLAUDE_DIR / "Logs" / "vector-service.log")
