"""
service.py — Memory Vector Service HTTP Daemon

stdlib http.server 實作；埠由 starter 傳入，單獨跑時走 lib.project_root.vector_service_port（config 有寫用 config，沒寫由資料根推）。
提供語意搜尋、索引、健康檢查等 API。

啟動: pythonw service.py  (Windows 背景)
      python service.py   (前景, 看 log)
"""

import json
import os
import signal
import sys
import threading
import time
import traceback
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

# Add parent dir to path for imports
SERVICE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SERVICE_DIR))
# import wg_core for centralized path resolution
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hooks"))   # 程式碼：本檔所在樹的 hooks/

from config import CLAUDE_DIR, extension_registrations, load_config, VECTORDB_DIR  # config.py 已把程式碼根放進 sys.path
from lib.project_root import vector_service_port
from indexer import NoEmbedderError, build_index, create_embedder, get_index_status
from searcher import search, search_raw, ranked_search, ranked_search_sections

# ─── Globals ─────────────────────────────────────────────────────────────────

_config: Dict[str, Any] = {}
_embedder = None
_start_time = 0.0
_port = 0
_request_count = 0
_index_lock = threading.Lock()
_index_status: Optional[Dict[str, Any]] = None  # None=idle, {"running":True,...} or result


def _ensure_ollama_running(config: Dict[str, Any]) -> None:
    """本機的 Ollama daemon 沒在跑（開機後沒人叫起來）就試著叫起來，最多等 15 秒；叫不起來照舊走降級（開場紅字）。
    只處理預設的本機位址；設了遠端或多後端（ollama_backends）就不碰。"""
    import urllib.request
    if os.environ.get("WG_NO_OLLAMA_AUTOSTART"):
        return
    base = str(config.get("ollama_base_url") or "").rstrip("/")
    if config.get("ollama_backends") or not base.startswith(("http://127.0.0.1", "http://localhost")):
        return

    def alive() -> bool:
        try:
            urllib.request.urlopen(base + "/api/tags", timeout=1.5).close()
            return True
        except Exception:
            return False

    if alive():
        return
    from lib.wg_platform import start_ollama_daemon
    result = start_ollama_daemon()
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [service] Ollama not running at {base}: start attempt -> {result}",
          file=sys.stderr, flush=True)
    if result != "started":
        return
    for _ in range(15):
        time.sleep(1.0)
        if alive():
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [service] Ollama is up", file=sys.stderr, flush=True)
            return


def _init_service():
    """Initialize service: load config and embedder."""
    global _config, _embedder, _start_time
    _config = load_config()
    _start_time = time.time()
    _ensure_ollama_running(_config)
    regs = extension_registrations()
    if regs["routes"] or regs["index_dirs"]:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [service] Extension routes: {sorted(regs['routes'])}; "
              f"index dirs: {sorted(regs['index_dirs'])}", file=sys.stderr, flush=True)

    try:
        _t = time.time()
        _embedder = create_embedder(_config)
        print(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} [service] Embedder loaded: "
            f"{_embedder.__class__.__name__} ({time.time() - _t:.1f}s)",
            file=sys.stderr, flush=True,
        )
    except Exception as e:
        print(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} [service] WARNING: "
            f"No embedder available: {e}",
            file=sys.stderr, flush=True,
        )
        _embedder = None


def _current_embedder():
    """這次搜尋用的 embedder。開機時沒有嵌入後端（Ollama 還沒裝、還沒起）就現在再建一次，建得起來就存回 _embedder：
    /health、/status 跟著報真實狀態，後面的請求也不必每次重建。建不起來照舊丟 NoEmbedderError（呼叫端回 503）。"""
    global _embedder
    if _embedder is None:
        _embedder = create_embedder(_config)
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [service] Embedder loaded after startup: {_embedder.__class__.__name__}",
              file=sys.stderr, flush=True)
    return _embedder


# ─── Request Handler ─────────────────────────────────────────────────────────

CLIENT_GONE = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)   # 呼叫端先斷線（多半是它自己逾時）


class VectorServiceHandler(BaseHTTPRequestHandler):
    """HTTP request handler for Memory Vector Service."""

    def log_message(self, format, *args):
        """Sanitized: strip query string from request line (議題 #6, 2026-04-28).

        log_request 路徑會以 format='"%s" %s %s', args=(request_line, code, size)
        呼叫；request_line 形如 'GET /search?q=<sensitive> HTTP/1.1'。此處將
        path 的 query string 截掉，其他 log 路徑（log_error / 直呼 log_message）
        不變。Parse 失敗 fail-open 保留原行為。
        """
        sanitized_args = args
        if format == '"%s" %s %s' and args:
            try:
                request_line = args[0]
                if isinstance(request_line, str):
                    parts = request_line.split(' ', 2)
                    if len(parts) >= 2:
                        method = parts[0]
                        path_clean = parts[1].split('?', 1)[0]
                        http_ver = parts[2] if len(parts) > 2 else 'HTTP/1.1'
                        sanitized_args = (f'{method} {path_clean} {http_ver}',) + tuple(args[1:])
            except Exception:
                sanitized_args = args  # fail-open
        try:
            msg = format % sanitized_args
        except Exception:
            msg = format  # fail-open: even formatting failure must not crash
        print(f"[service] {self.client_address[0]} - {msg}", file=sys.stderr)

    def handle_one_request(self):
        """呼叫端先斷線（回應寫到一半）：沒有人收得到回覆，不印 traceback、不再試回 500，只記一行（不帶查詢字串）。"""
        try:
            super().handle_one_request()
        except CLIENT_GONE as e:
            path = urlparse(getattr(self, "path", "") or "").path
            print(f"[service] client gone before the reply was sent ({type(e).__name__}): {path}", file=sys.stderr)

    def _send_json(self, data: Any, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, message: str):
        self._send_json({"error": message}, status)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length > 0 else b""

    def _parse_json_body(self) -> Optional[Dict]:
        try:
            body = self._read_body()
            return json.loads(body) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    # ── Route dispatcher ──

    def do_GET(self):
        global _request_count
        _request_count += 1

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)

        routes = {
            "/search": self._handle_search,
            "/search/ranked": self._handle_search_ranked,
            "/search/ranked-sections": self._handle_search_ranked_sections,
            "/health": self._handle_health,
            "/status": self._handle_status,
        }

        handler = routes.get(path)
        ext_route = None if handler else extension_registrations()["routes"].get(path)   # 開著的擴充登記的路徑
        if ext_route is not None:
            handler = lambda p: self._handle_extension_route(ext_route, p)   # noqa: E731
        if handler:
            try:
                handler(params)
            except CLIENT_GONE:            # 呼叫端已斷線：交給 handle_one_request 記一行，不當程式錯誤
                raise
            except NoEmbedderError as e:   # 沒有嵌入後端＝服務沒就緒、不是程式壞了：回 503 不印 traceback，呼叫端照沒就緒安靜退回
                self._send_error(503, str(e))
            except Exception as e:
                traceback.print_exc(file=sys.stderr)
                self._send_error(500, str(e))
        else:
            self._send_error(404, f"Not found: {path}")

    def do_POST(self):
        global _request_count
        _request_count += 1

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        routes = {
            "/index": self._handle_index,
            "/index/incremental": self._handle_index_incremental,
            "/reload": self._handle_reload,
            "/shutdown": self._handle_shutdown,
            # enhanced search / rerank / extract endpoints
            "/search/enhanced": self._handle_search_enhanced,
            "/rerank": self._handle_rerank,
            "/extract": self._handle_extract,
        }

        handler = routes.get(path)
        if handler:
            try:
                handler()
            except CLIENT_GONE:
                raise
            except Exception as e:
                traceback.print_exc(file=sys.stderr)
                self._send_error(500, str(e))
        else:
            self._send_error(404, f"Not found: {path}")

    def do_OPTIONS(self):
        """CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ── GET handlers ──

    def _handle_search(self, params: Dict):
        q = params.get("q", [""])[0]
        if not q:
            self._send_error(400, "Missing query parameter 'q'")
            return

        top_k = int(params.get("top_k", [str(_config.get("search_top_k", 5))])[0])
        min_score = float(params.get("min_score", [str(_config.get("search_min_score", 0.65))])[0])
        layer = params.get("layer", ["all"])[0]
        user = params.get("user", [""])[0] or None
        roles_raw = params.get("roles", [""])[0]
        roles = [r.strip() for r in roles_raw.split(",") if r.strip()] or None
        # layers=global,shared:c--proj,...：只搜這幾層（write-gate 去重用）
        layers_raw = params.get("layers", [""])[0]
        layers = [l.strip() for l in layers_raw.split(",") if l.strip()] or None

        results = search(
            query=q,
            config=_config,
            top_k=top_k,
            min_score=min_score,
            layer_filter=layer if layer != "all" else None,
            embedder=_current_embedder(),
            user=user,
            roles=roles,
            layers=layers,
        )
        self._send_json(results)

    def _handle_search_ranked(self, params: Dict):
        """GET /search/ranked?q=...&intent=general&top_k=5&min_score=0.50&user=&roles="""
        q = params.get("q", [""])[0]
        if not q:
            self._send_error(400, "Missing query parameter 'q'")
            return

        intent = params.get("intent", ["general"])[0]
        top_k = int(params.get("top_k", [str(_config.get("search_top_k", 5))])[0])
        min_score = float(params.get("min_score", ["0.50"])[0])
        layer = params.get("layer", ["all"])[0]
        user = params.get("user", [""])[0] or None
        roles_raw = params.get("roles", [""])[0]
        roles = [r.strip() for r in roles_raw.split(",") if r.strip()] or None
        layers_raw = params.get("layers", [""])[0]
        layers = [l.strip() for l in layers_raw.split(",") if l.strip()] or None

        results = ranked_search(
            query=q,
            config=_config,
            intent=intent,
            top_k=top_k,
            min_score=min_score,
            layer_filter=layer if layer != "all" else None,
            embedder=_current_embedder(),
            user=user,
            roles=roles,
            layers=layers,
        )
        self._send_json(results)

    def _handle_search_ranked_sections(self, params: Dict):
        """GET /search/ranked-sections?q=...&intent=general&top_k=5&max_sections=3&user=&roles="""
        q = params.get("q", [""])[0]
        if not q:
            self._send_error(400, "Missing query parameter 'q'")
            return

        intent = params.get("intent", ["general"])[0]
        top_k = int(params.get("top_k", [str(_config.get("search_top_k", 5))])[0])
        max_sections = int(params.get("max_sections", ["3"])[0])
        min_score = float(params.get("min_score", ["0.50"])[0])
        layer = params.get("layer", ["all"])[0]
        user = params.get("user", [""])[0] or None
        roles_raw = params.get("roles", [""])[0]
        roles = [r.strip() for r in roles_raw.split(",") if r.strip()] or None
        layers_raw = params.get("layers", [""])[0]
        layers = [l.strip() for l in layers_raw.split(",") if l.strip()] or None

        results = ranked_search_sections(
            query=q,
            config=_config,
            intent=intent,
            top_k=top_k,
            max_sections=max_sections,
            min_score=min_score,
            layer_filter=layer if layer != "all" else None,
            embedder=_current_embedder(),
            user=user,
            roles=roles,
            layers=layers,
        )
        self._send_json(results)

    def _handle_extension_route(self, func, params: Dict):
        """擴充登記的 GET 路徑：func(params, ctx) 回傳 JSON 可序列化的值；ValueError＝呼叫端參數錯（400）。
        ctx：config（vector_search 設定）、embedder（這次搜尋用的；沒有嵌入後端時丟 NoEmbedderError → 503）。"""
        try:
            data = func(params, {"config": _config, "embedder": _current_embedder()})
        except ValueError as e:
            self._send_error(400, str(e))
            return
        self._send_json(data)

    def _handle_health(self, params: Dict):
        """只回記憶體裡的狀態（不開 LanceDB）：開場用它判沒有嵌入後端、是不是別的資料根的服務，不必等 /status。"""
        self._send_json({
            "status": "ok",
            "embedder": _embedder.__class__.__name__ if _embedder else "none",
            "data_root": str(CLAUDE_DIR),
            "uptime_seconds": round(time.time() - _start_time, 1),
        })

    def _handle_status(self, params: Dict):
        index_status = get_index_status(_config)
        result = {
            "service": {
                "uptime_seconds": round(time.time() - _start_time, 1),
                "requests_served": _request_count,
                "embedder": _embedder.__class__.__name__ if _embedder else "none",
                "port": _port,
                "data_root": str(CLAUDE_DIR),
            },
            "index": index_status,
            "config": {
                "embedding_backend": _config.get("embedding_backend"),
                "embedding_model": _config.get("embedding_model"),
                "search_top_k": _config.get("search_top_k"),
                "search_min_score": _config.get("search_min_score"),
            },
        }
        if _index_status:
            result["index_job"] = _index_status
        self._send_json(result)

    # ── POST handlers ──

    def _handle_index(self):
        self._start_index_background(incremental=False)

    def _handle_index_incremental(self):
        self._start_index_background(incremental=True)

    def _start_index_background(self, incremental: bool):
        global _index_status
        if _index_lock.locked():
            self._send_json({"status": "already_running", "current": _index_status})
            return
        _index_status = {"running": True, "incremental": incremental, "started_at": time.time()}

        def _run():
            global _index_status
            with _index_lock:
                try:
                    stats = build_index(_config, incremental=incremental, verbose=True)
                    _index_status = {"running": False, "result": stats, "finished_at": time.time()}
                except Exception as e:
                    _index_status = {"running": False, "error": str(e), "finished_at": time.time()}

        threading.Thread(target=_run, daemon=True).start()
        self._send_json({"status": "started", "incremental": incremental})

    def _handle_reload(self):
        global _config, _embedder
        _config = load_config()
        # Clear ollama_client singleton so new config (e.g. a remote backend) takes effect
        try:
            TOOLS_DIR = SERVICE_DIR.parent
            if str(TOOLS_DIR) not in sys.path:
                sys.path.insert(0, str(TOOLS_DIR))
            import ollama_client
            ollama_client._client_instance = None
        except Exception:
            pass
        try:
            _embedder = create_embedder(_config)
        except Exception as e:
            self._send_error(500, f"Failed to reload embedder: {e}")
            return
        self._send_json({"status": "reloaded", "embedder": _embedder.__class__.__name__})

    def _handle_shutdown(self):
        self._send_json({"status": "shutting_down"})
        threading.Timer(0.5, lambda: os._exit(0)).start()

    # ── enhanced search / rerank / extract handlers ──

    def _handle_search_enhanced(self):
        body = self._parse_json_body()
        if not body or "q" not in body:
            self._send_error(400, "Missing 'q' in body")
            return
        try:
            from reranker import enhanced_search
            results = enhanced_search(body["q"], _config, _embedder, top_k=body.get("top_k", 5))
            self._send_json(results)
        except ImportError:
            self._send_error(501, "reranker module not available")
        except Exception as e:
            self._send_error(500, str(e))

    def _handle_rerank(self):
        body = self._parse_json_body()
        if not body or "q" not in body:
            self._send_error(400, "Missing 'q' in body")
            return
        try:
            from reranker import rerank
            results = rerank(body["q"], _config, _embedder, candidates=body.get("candidates"))
            self._send_json(results)
        except ImportError:
            self._send_error(501, "reranker module not available")
        except Exception as e:
            self._send_error(500, str(e))

    def _handle_extract(self):
        body = self._parse_json_body()
        if not body or "text" not in body:
            self._send_error(400, "Missing 'text' in body")
            return
        try:
            from reranker import extract_knowledge
            results = extract_knowledge(body["text"], _config, _embedder)
            self._send_json(results)
        except ImportError:
            self._send_error(501, "reranker module not available")
        except Exception as e:
            self._send_error(500, str(e))


# ─── Server Startup ──────────────────────────────────────────────────────────


PID_WATCH_INTERVAL_S = 30


def _watch_pid_file(pid_file: Path, interval_s: float = PID_WATCH_INTERVAL_S, exit_fn=os._exit) -> None:
    """pid 檔不見了就結束：每個資料根一支服務，資料根被清掉（測試的暫存根、搬走的安裝）後不留孤兒行程。"""
    while pid_file.exists():
        time.sleep(interval_s)
    exit_fn(0)


def run_server(port: int):
    """Start the HTTP daemon.

    ThreadingHTTPServer：每請求獨立執行緒，長請求（/rerank、/search/enhanced 等
    走 LLM 的路徑）不會 block /health，starter 才不會把忙碌中的健康服務誤判 hang 死。
    執行緒安全依據：LanceDB 每請求各自 connect/open_table（讀走版本快照）；
    索引寫入已由 _index_lock 序列化在單一背景執行緒；OllamaClient 每呼叫獨立
    HTTP request；_request_count 為 best-effort 計數（競態僅影響統計值）。
    """
    global _port
    _port = port
    _init_service()

    server = ThreadingHTTPServer(("127.0.0.1", port), VectorServiceHandler)
    server.daemon_threads = True
    print(f"[service] Memory Vector Service listening on http://127.0.0.1:{port}", file=sys.stderr)

    # Write PID file for management
    pid_file = VECTORDB_DIR / "service.pid"
    VECTORDB_DIR.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()), newline="\n")
    threading.Thread(target=_watch_pid_file, args=(pid_file,), daemon=True).start()

    def cleanup(signum=None, frame=None):
        print(f"\n[service] Shutting down...", file=sys.stderr)
        try:
            pid_file.unlink(missing_ok=True)
        except Exception:
            pass
        # server.shutdown() 會阻塞等 serve_forever 迴圈退出；signal handler 跑在
        # 主執行緒（= serve_forever 所在執行緒），同執行緒同步呼叫會互等死鎖
        # （POSIX 面；Windows SIGTERM 面亦同理）→ 改由獨立 thread 觸發。
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        cleanup()


if __name__ == "__main__":
    # stdout／stderr 接到 vector-service.log：Windows 子行程預設用系統編碼（cp950），
    # 卡名有它編不出的字時 indexer 的 print 會丟例外、整輪索引中止。
    for _stream in (sys.stdout, sys.stderr):
        if _stream is not None and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    import argparse
    _ap = argparse.ArgumentParser(description="Vector search service (HTTP, localhost).")
    _ap.add_argument("port", nargs="?", default="0", help="listen port (default: from config)")
    _a = _ap.parse_args()                  # --help 只印說明就結束，不會起伺服器
    try:
        port = int(_a.port)
    except ValueError:
        port = 0
    run_server(port or vector_service_port())
