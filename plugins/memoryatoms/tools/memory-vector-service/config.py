"""
config.py — Memory Vector Service 設定管理

讀取資料根（WG_CLAUDE_DIR 或 ~/.claude）workflow/config.json 的 vector_search 區塊。
所有設定都有合理預設值，config.json 不存在時也能正常運作。
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict

_CODE_ROOT = Path(__file__).resolve().parents[2]   # 程式碼根：lib.* 從本樹載
if str(_CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(_CODE_ROOT))
from lib.project_root import claude_data_dir  # noqa: E402
from lib.edition import layered_config  # noqa: E402  讀：插件版出廠設定墊底；寫（save_config）照寫資料根

CLAUDE_DIR = claude_data_dir()   # 使用者資料根（WG_CLAUDE_DIR 或 ~/.claude），和 hooks 同一支算法
CONFIG_PATH = CLAUDE_DIR / "workflow" / "config.json"
VECTORDB_DIR = CLAUDE_DIR / "memory" / "_vectordb"

# 沒有預設值、有寫才帶的鍵。埠不在 DEFAULTS：沒寫 service_port 時由 lib.project_root.vector_service_port 從資料根推
_PASSTHROUGH_KEYS = ("additional_atom_dirs", "ollama_backends", "service_port")

DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "embedding_backend": "ollama",          # "ollama" | "sentence-transformers"
    "embedding_model": "qwen3-embedding",   # Ollama model name
    "fallback_backend": "none",
    "fallback_model": "none",
    "ollama_base_url": "http://127.0.0.1:11434",
    "ollama_llm_model": "qwen3:1.7b",
    "search_top_k": 5,
    "search_min_score": 0.65,
    "search_timeout_ms": 3500,              # hook 端（wg_atoms）等搜尋回應的上限；服務本身不讀，兩邊預設同一個
    "auto_start_service": True,
    "auto_index_on_change": True,
    "index_distant": False,                 # 是否索引 _distant/ 遙遠記憶
    "additional_atom_dirs": [],              # 額外 atom 來源目錄
}


def load_config() -> Dict[str, Any]:
    """Load vector_search config with defaults fallback."""
    config = dict(DEFAULTS)
    vs = layered_config(CONFIG_PATH).get("vector_search", {})
    if isinstance(vs, dict):
        config.update({k: v for k, v in vs.items() if k in DEFAULTS or k in _PASSTHROUGH_KEYS})
    return config


_EMPTY_REGISTRATIONS: Dict[str, Any] = {"index_dirs": {}, "type_bonus": {}, "routes": {}}
_registrations: Dict[str, Any] = {}


def extension_registrations() -> Dict[str, Any]:
    """開著的擴充給向量服務的登記（hooks/wg_plugins.vector_registrations）；一個行程讀一次。
    讀不了（擴充機制本身壞掉）→ 當作沒有擴充，stderr 一行，服務與索引照常。"""
    if _registrations:
        return _registrations
    try:
        for sub in ("hooks", "lib"):
            entry = str(_CODE_ROOT / sub)
            if entry not in sys.path:
                sys.path.append(entry)
        import wg_plugins
        _registrations.update(wg_plugins.vector_registrations())
    except Exception as e:  # noqa: BLE001
        print(f"[service] extension registrations unavailable, running without them: {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)
    for key, empty in _EMPTY_REGISTRATIONS.items():
        _registrations.setdefault(key, dict(empty))
    return _registrations


def save_config(config: Dict[str, Any]) -> None:
    """Save vector_search config back to config.json (merge, not overwrite)."""
    full: Dict[str, Any] = {}
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                full = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    full["vector_search"] = {k: v for k, v in config.items() if k in DEFAULTS or k in _PASSTHROUGH_KEYS}
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(full, f, indent=2, ensure_ascii=False)
