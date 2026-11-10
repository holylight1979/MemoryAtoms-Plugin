"""
wg_atoms_vector.py — 向量搜尋：確保向量服務可用（含冷卻重踢）、語意搜尋、觀測 log、觸發增量建索引。

誰叫它：ups_search、memory_search、vcs 同步後的增量索引。
純搬移自 wg_atoms.py，函式本體未改；hooks/wg_atoms.py 仍 re-export 這裡的名稱。
"""

import json
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
from wg_core import CLAUDE_DIR, CODE_DIR, WORKFLOW_DIR, _atom_debug_error, vector_service_port
from pathlib import Path


# ─── Vector Observation Log + Semantic Search (was wg_intent) ───────────────

_VECTOR_OBS_LOG = CLAUDE_DIR / "Logs" / "vector-observation.log"


_vector_obs_logger: Optional[Any] = None    # logging.Logger；logging 約 30 ms，用到才載（HOOK_IMPORT_ON_USE）


_vector_obs_logger_failed: bool = False


def _get_vector_obs_logger() -> Optional[Any]:
    global _vector_obs_logger, _vector_obs_logger_failed
    if _vector_obs_logger is not None:
        return _vector_obs_logger
    if _vector_obs_logger_failed:
        return None
    try:
        import logging
        import logging.handlers

        _VECTOR_OBS_LOG.parent.mkdir(parents=True, exist_ok=True)
        lg = logging.getLogger("wg.vector_obs")
        lg.setLevel(logging.INFO)
        lg.propagate = False
        if not lg.handlers:
            h = logging.handlers.RotatingFileHandler(
                str(_VECTOR_OBS_LOG),
                maxBytes=2_000_000,
                backupCount=3,
                encoding="utf-8",
                delay=True,
            )
            h.setFormatter(logging.Formatter("%(message)s"))
            lg.addHandler(h)
        _vector_obs_logger = lg
        return lg
    except Exception as e:
        _atom_debug_error("vector_obs:logger_init", e)
        _vector_obs_logger_failed = True
        return None


def _log_vector_obs(
    session_id: Optional[str],
    fn: str,
    flag_state: str,
    result_count: int,
    fallback_used: bool,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    lg = _get_vector_obs_logger()
    if lg is None:
        return
    rec: Dict[str, Any] = {
        "ts": time.time(),
        "session_id": session_id or "",
        "fn": fn,
        "flag_state": flag_state,
        "result_count": result_count,
        "fallback_used": fallback_used,
    }
    if extra:
        rec.update(extra)
    try:
        lg.info(json.dumps(rec, ensure_ascii=False))
    except Exception as e:
        _atom_debug_error("vector_obs:write", e)


_REKICK_MARKER = WORKFLOW_DIR / "vector_rekick.marker"


_REKICK_COOLDOWN_S = 120.0



def _vector_opted_in() -> bool:
    """插件版沒跑 setup-vector（venv 裡沒有 lancedb）→ 不起 starter（D3：向量點頭才裝、才起服務）；安裝版一律 True。"""
    try:
        import vector_venv   # lib/
        return not vector_venv.plugin_edition() or vector_venv.lancedb_available()
    except Exception as e:   # 判不了照舊起（starter 自己會再判一次），留帳
        _atom_debug_error("vector:opted_in", e)
        return True

def _ensure_vector_ready(
    session_id: Optional[str],
    *,
    flag_path: Optional[Path] = None,
    marker_path: Optional[Path] = None,
    spawn: bool = True,
    wait_s: float = 0.3,
) -> Tuple[bool, bool]:
    """flag 缺失時的 UPS 端自癒。回 (ready, kicked)。

    fire-and-forget spawn starter.py（cooldown 防同 session 連環 spawn），再短等
    ≤wait_s 一次性補救「服務活著只是 flag 遺失」類（starter 首次 health 成功即回寫
    flag，毫秒級）；真冷啟動秒級以上，本輪照舊 fallback、下一 prompt 收割。
    """
    flag = flag_path or (WORKFLOW_DIR / "vector_ready.flag")
    if flag.exists():
        return True, False
    marker = marker_path or _REKICK_MARKER
    kicked = False
    try:
        stale = (
            not marker.exists()
            or time.time() - marker.stat().st_mtime > _REKICK_COOLDOWN_S
        )
        if stale and spawn and _vector_opted_in():
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(str(time.time()), encoding="utf-8", newline="\n")
            import subprocess
            starter = CODE_DIR / "tools" / "memory-vector-service" / "starter.py"
            kw: Dict[str, Any] = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            }
            if sys.platform == "win32":
                kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
            else:
                kw["start_new_session"] = True
            subprocess.Popen(
                [sys.executable, str(starter),
                 "--phase", "ups_rekick", "--session-id", session_id or ""],
                **kw,
            )
            kicked = True
    except Exception as e:
        _atom_debug_error("vector:rekick", e)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        time.sleep(0.1)
        if flag.exists():
            return True, kicked
    return False, kicked


def _semantic_search(
    prompt: str, config: Dict[str, Any], intent: str = "general",
    user: Optional[str] = None,
    roles: Optional[List[str]] = None,
    session_id: Optional[str] = None,
    layers: Optional[List[str]] = None,
) -> List[Tuple[str, str, List[str], List[Dict]]]:
    """Query Memory Vector Service with intent-aware ranked search.

    layers：可見 layer 白名單（visible_vector_layers），服務端只在這幾層查；
    user/roles 仍一併送，給尚未支援 layers 的舊服務退回 role clause。"""
    import urllib.error
    import urllib.parse
    import urllib.request

    vs_config = config.get("vector_search", {})
    if not vs_config.get("enabled", True):
        _log_vector_obs(session_id, "_semantic_search", "disabled", 0, True,
                        extra={"intent": intent})
        return []
    _ready, _kicked = _ensure_vector_ready(session_id)
    if not _ready:
        _log_vector_obs(session_id, "_semantic_search", "no_flag", 0, True,
                        extra={"intent": intent, "rekicked": _kicked})
        return []
    port = vector_service_port(config)
    top_k = vs_config.get("search_top_k", 5)
    min_score = vs_config.get("search_min_score", 0.65)
    timeout_s = vs_config.get("search_timeout_ms", 3500) / 1000.0

    try:
        def _add_identity(p: Dict[str, Any]) -> Dict[str, Any]:
            if user:
                p["user"] = user
            if roles:
                p["roles"] = ",".join(roles)
            if layers:
                p["layers"] = ",".join(layers)
            return p

        use_sections = True
        params_dict = _add_identity({
            "q": prompt, "top_k": top_k,
            "min_score": min_score,
            "intent": intent,
            "max_sections": 3,
        })
        params = urllib.parse.urlencode(params_dict)
        url = f"http://127.0.0.1:{port}/search/ranked-sections?{params}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                results = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                use_sections = False
                params_dict = _add_identity({
                    "q": prompt, "top_k": top_k,
                    "min_score": min_score,
                    "intent": intent,
                })
                params = urllib.parse.urlencode(params_dict)
                url = f"http://127.0.0.1:{port}/search/ranked?{params}"
                req = urllib.request.Request(url, headers={"Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                    results = json.loads(resp.read())
            else:
                raise

        entries: List[Tuple[str, str, List[str], List[Dict]]] = []
        seen = set()
        for r in results:
            name = r.get("atom_name", "")
            if name and name not in seen:
                sections = r.get("sections", []) if use_sections else []
                entries.append((name, r.get("file_path", ""), [], sections))
                seen.add(name)
        _log_vector_obs(
            session_id, "_semantic_search", "ready", len(entries), False,
            extra={"intent": intent, "use_sections": use_sections},
        )
        return entries
    except Exception as e:
        # 服務在、但沒有嵌入後端（503）＝沒就緒，不是錯誤：開場紅字已經講過，這裡安靜退回 BM25，
        # 只在觀測 log 記一筆、不進 atom-debug 錯誤紀錄；旗標留著（服務活著），裝好後端後下一句就照常查
        if isinstance(e, urllib.error.HTTPError) and e.code == 503:
            _log_vector_obs(session_id, "_semantic_search", "no_embedder", 0, True, extra={"intent": intent})
            return []
        # 連不上＝服務掛了：拿掉就緒旗標，下一句走 _ensure_vector_ready 自癒（重踢＋最多等 0.3 秒），不再每句白等連線失敗
        if isinstance(getattr(e, "reason", None), ConnectionRefusedError):
            try:
                (WORKFLOW_DIR / "vector_ready.flag").unlink(missing_ok=True)
            except OSError as flag_err:
                _atom_debug_error("注入:_semantic_search:flag", flag_err)
        _atom_debug_error("注入:_semantic_search", e)
        _log_vector_obs(
            session_id, "_semantic_search", "error", 0, True,
            extra={"intent": intent, "err": str(e)[:120]},
        )
        return []


def _trigger_incremental_index(config: Dict[str, Any]) -> None:
    """Non-blocking request to re-index changed atoms."""
    import urllib.request

    vs_config = config.get("vector_search", {})
    if not vs_config.get("auto_index_on_change", True):
        return
    port = vector_service_port(config)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/index/incremental",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=1)
    except Exception as e:
        _atom_debug_error("注入:_trigger_incremental_index", e)
