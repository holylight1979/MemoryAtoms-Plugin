"""wg_plugins.py — Python 這側的插件點：有登記表就掛，沒有就略過；核心程式不寫任何插件的名字。

登記表是擴充資料夾裡的 memoryatoms-extension.json（舊名 plugin.json 照讀、印一行遷移提示），整份攤開就看得懂（沒有的面就不寫）：

    {
      "name": "<名稱>",                              和資料夾同名
      "description": "一句話說它做什麼",
      "python": {
        "path": ["hooks"],                           相對插件資料夾；加進 sys.path，模組與它的同伴從這裡載
        "on": {"PreToolUse": "模組:函式"},            插件點 → 誰來接
        "state": ["鍵"]                               插件自己在 state 裡用的鍵：核心重建 state（compact／resume 時專案根換了）照搬
      },
      "config": {"<設定段>": {...}},                 出廠預設：開著的插件才墊進 wg_core.load_config，使用者的 config.json／
                                                     config.local.json 同名鍵優先（段是物件就淺合併）；兩個插件給同一段算登記表錯
      "vector": {                                    向量服務那側（服務啟動時讀；見下方 vector_registrations）
        "path": ["vector"],                          相對插件資料夾；服務行程加進 sys.path
        "index_dirs": [{"dir": "<資料夾名>", "type": "<Type 值>", "type_bonus": {"<意圖>": 0.05}}],
                                                     記憶層裡這個名字的子資料夾也進索引；檔頭 Type 為該值的列算這一型，
                                                     type_bonus 是排序時這一型在該意圖的加分
        "routes": {"/路徑": "模組:函式"}              GET 路徑 → 函式(params, ctx)，回傳 JSON 可序列化的值；ValueError＝400
      },
      "mcp": "mcp/index.js",                         Node 模組（匯出 register(api)），見 lib/plugins.js
      "settings_fragment": "settings.fragment.json"  安裝器併進 settings 的片段（原生帶不了的：statusLine）
    }

擴充在哪、開著沒：lib/extension_dirs.py（安裝版掃 <程式碼根>/extensions/；插件版讀 Claude Code 的 plugins/installed_plugins.json
找 memoryatoms-<名>，再看 enabledPlugins）。擴充的 skills/、hooks/hooks.json 由 Claude Code 自己載，核心不碰。
擴充模組載入前，本模組把 WG_CORE_ROOT 設成核心程式碼根（擴充裡找核心 lib 的地方先看它，沒有才退回安裝版的相對位置）。

插件點（核心呼叫 emit 的地方）；函式收一個 ctx（dict），回傳值怎麼用看最後一欄：

    插件點                 什麼時候                      ctx                                              回傳
    SessionStart             每次開場（含 resume、compact）  state config session_id input_data source         要送的行
    SessionStart.new_session 開新 session 才跑               state config session_id input_data source         要送的行
    UserPromptSubmit         組這一句的 context             state config session_id prompt（使用者打的字）    要送的行；或 {"lines": [...],
                                                            query（這句拿去檢索的文字） cwd                    "reserve_tokens": n}＝送這些行並從這句的預算保留 n
    UserPromptSubmit.intent  使用者打字的那句，判完意圖後    intent state config session_id                    要送的行
    PreToolUse               工具執行前                     input_data config session_id tool_name tool_input 提示文字（有 deny 時只進 stderr）
    PreToolUse.warned        上一點的提示真的送出時         同上＋warnings                                    不用
    PostToolUse.edited       Edit／Write 改完一個檔         state config session_id tool_name file_path       不用
    PostToolUse.saved        state 落盤之後                 config session_id tool_name file_path             提示文字
    PreCompact               壓縮前                         state config session_id input_data                不用
    PostCompact              壓縮後（輸入帶 compact_summary） state config session_id input_data              不用
    PostToolBatch            一批工具呼叫都解析完（每批都跑） state config session_id input_data              要送的行
    Stop.block_note          收尾閘要擋時，附在擋訊息尾巴   state config transcript transcript_text           文字
    SessionEnd               session 結束                   state config session_id cwd staging_dir           不用
    AecReport.saved          收尾報告落檔後                 severity state config session_id                  True＝看得到，核心不用補訊號
    AecReport.unseen         Stop 補訊號前再問一次          severity report_file config session_id            True＝看得到或已告知使用者
    HealthWeekly.freshness   週健檢算管線鮮度時（tools/health-weekly.py） fresh_cut fresh_days sessions_active  {"red": [...], "yellow": [...], "info": [...]}

回傳 None＝沒話說；list 攤平。壞掉（登記表格式錯、模組載不起來、函式丟例外）核心照跑，stderr 一行 [Guardian:Degraded]。
沒有登記表的資料夾靜默略過（extensions/.claude-plugin/ 放的是市集檔）。
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

CORE_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = CORE_ROOT / "extensions"   # 安裝版的擴充資料夾（插件版不看它，見 lib/extension_dirs.py）

_manifests: Optional[List[Dict[str, Any]]] = None      # 一支 hook 一個行程，掃一次就夠
_functions: Dict[str, Optional[Callable[[Dict[str, Any]], Any]]] = {}   # "模組:函式" → 函式；載不起來記 None，不重試不重印
_warned: set = set()


def _warn(name: str, why: str) -> None:
    """一個行程、一個壞插件只印一行（第一次壞在哪）。"""
    if name in _warned:
        return
    _warned.add(name)
    line = f"[Guardian:Degraded] extension {name} failed to load: {why}; the core keeps running without it"
    try:
        from i18n import t
        line = t("hook.wg_plugins.broken", name=name, why=why)
    except Exception:  # noqa: BLE001  語言表載不起來也要看得到這一行
        pass
    try:
        if sys.stderr:  # pythonw 下 sys.stderr 可能是 None
            sys.stderr.write(line + "\n")
    except (OSError, ValueError):
        pass


def _manifest_problem(manifest: Any, folder: str) -> str:
    """登記表哪裡不對（空字串＝沒問題）；只驗 emit 會碰到的欄位。"""
    if not isinstance(manifest, dict):
        return "plugin.json is not an object"
    if manifest.get("name") != folder:
        return f"plugin.json name {manifest.get('name')!r} differs from folder"
    if not isinstance(manifest.get("config", {}), dict):
        return "config is not an object"
    python = manifest.get("python")
    if python is None:
        return ""
    if not isinstance(python, dict):
        return "python is not an object"
    on, paths, state = python.get("on", {}), python.get("path", []), python.get("state", [])
    if not isinstance(on, dict) or not all(isinstance(v, str) and ":" in v for v in on.values()):
        return 'python.on must map plug point to "module:function"'
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        return "python.path must be a list of strings"
    if not isinstance(state, list) or not all(isinstance(k, str) for k in state):
        return "python.state must be a list of strings"
    return _vector_problem(manifest.get("vector"))


def _vector_problem(vector: Any) -> str:
    """登記表 vector 欄哪裡不對（空字串＝沒問題或沒寫）。"""
    if vector is None:
        return ""
    if not isinstance(vector, dict):
        return "vector is not an object"
    paths, dirs, routes = vector.get("path", []), vector.get("index_dirs", []), vector.get("routes", {})
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        return "vector.path must be a list of strings"
    if not isinstance(dirs, list) or not all(isinstance(d, dict) and isinstance(d.get("dir"), str) and d["dir"]
                                             for d in dirs):
        return 'vector.index_dirs must be a list of {"dir": "<name>", ...}'
    for d in dirs:
        if not isinstance(d.get("type", ""), str) or not isinstance(d.get("type_bonus", {}), dict):
            return "vector.index_dirs[].type must be a string and type_bonus an object"
    if not isinstance(routes, dict) or not all(isinstance(k, str) and k.startswith("/") and isinstance(v, str) and ":" in v
                                               for k, v in routes.items()):
        return 'vector.routes must map "/path" to "module:function"'
    return ""


def _load_manifests() -> List[Dict[str, Any]]:
    global _manifests
    if _manifests is not None:
        return _manifests
    import extension_dirs   # lib/（wg_core 已放進 sys.path）
    found: List[Dict[str, Any]] = []
    for ext in extension_dirs.extensions(_warn, PLUGINS_DIR):
        if not ext.enabled:
            continue
        file = extension_dirs.registry_file(ext.folder)
        if file is None:
            continue
        try:
            manifest = json.loads(file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            _warn(ext.name, f"{file.name}: {e}")
            continue
        problem = _manifest_problem(manifest, ext.name)
        if problem:
            _warn(ext.name, problem)
            continue
        manifest["_dir"] = ext.folder
        found.append(manifest)
    if found:
        os.environ.setdefault("WG_CORE_ROOT", str(CORE_ROOT))   # 擴充模組與它起的子行程從這裡找核心
    _manifests = found
    return found


def _function(manifest: Dict[str, Any], ref: str) -> Optional[Callable[[Dict[str, Any]], Any]]:
    if ref in _functions:
        return _functions[ref]
    module_name, _, func_name = ref.partition(":")
    for rel in manifest["python"].get("path", []):
        entry = str(manifest["_dir"] / rel)
        if entry not in sys.path:
            sys.path.append(entry)      # 排在後面：插件的同名模組蓋不掉核心
    func = None
    try:
        func = getattr(importlib.import_module(module_name), func_name)
    except (Exception, SystemExit) as e:  # noqa: BLE001
        _warn(manifest["name"], f"load {ref}: {type(e).__name__}: {e}")
    _functions[ref] = func
    return func


def config_defaults() -> Dict[str, Any]:
    """開著的插件帶的出廠預設（plugin.json 的 config 欄）合成一份，給 wg_core.load_config 墊在使用者設定底下。
    照資料夾名排序；兩個插件給同一個頂層鍵 → 先到的算數，後到的 stderr 一行（登記表寫重了）。"""
    merged: Dict[str, Any] = {}
    owner: Dict[str, str] = {}
    for manifest in _load_manifests():
        for key, value in (manifest.get("config") or {}).items():
            if key in owner:
                _warn(manifest["name"], f"config key {key!r} is already given by plugin {owner[key]}")
                continue
            merged[key], owner[key] = value, manifest["name"]
    return merged


def state_keys() -> List[str]:
    """開著的插件在 state 裡自己用的鍵（plugin.json 的 python.state）：核心重建 state 時照搬（handlers/session_start_state.carry_on_rebuild）。"""
    return [key for manifest in _load_manifests() for key in (manifest.get("python") or {}).get("state", [])]


def vector_registrations() -> Dict[str, Any]:
    """開著的插件給向量服務的登記（登記表的 vector 欄）合成一份；沒有插件登記時三項都是空的。
        index_dirs  {資料夾名: {"type": ..., "type_bonus": {...}}}   indexer 掃記憶層時不略過這些子資料夾
        type_bonus  {(type, 意圖): 加分}                              searcher 排序加分
        routes      {"/路徑": 函式}                                   service 的 GET 路由；函式載不起來的不收（stderr 一行）
    同一路徑或資料夾兩個插件都登記 → 先到的算數，後到的 stderr 一行。"""
    out: Dict[str, Any] = {"index_dirs": {}, "type_bonus": {}, "routes": {}}
    owner: Dict[str, str] = {}
    for manifest in _load_manifests():
        vector = manifest.get("vector") or {}
        for entry in vector.get("index_dirs", []):
            key = "dir " + entry["dir"]
            if key in owner:
                _warn(manifest["name"], f"vector index dir {entry['dir']!r} is already registered by plugin {owner[key]}")
                continue
            owner[key] = manifest["name"]
            out["index_dirs"][entry["dir"]] = {"type": entry.get("type", ""), "type_bonus": entry.get("type_bonus", {})}
            for intent, bonus in (entry.get("type_bonus") or {}).items():
                if entry.get("type") and isinstance(bonus, (int, float)):
                    out["type_bonus"][(entry["type"], intent)] = float(bonus)
        for path, ref in (vector.get("routes") or {}).items():
            key = "route " + path
            if key in owner:
                _warn(manifest["name"], f"vector route {path!r} is already registered by plugin {owner[key]}")
                continue
            func = _function({**manifest, "python": {"path": vector.get("path", [])}}, ref)
            if func is not None:
                owner[key] = manifest["name"]
                out["routes"][path] = func
    return out


def emit(point: str, **ctx: Any) -> List[Any]:
    """呼叫掛在 point 上的每個函式（照插件資料夾名排序），收回它們的回傳值。"""
    results: List[Any] = []
    for manifest in _load_manifests():
        ref = ((manifest.get("python") or {}).get("on") or {}).get(point)
        func = _function(manifest, ref) if ref else None
        if func is None:
            continue
        try:
            out = func(ctx)
        except (Exception, SystemExit) as e:  # noqa: BLE001
            _warn(manifest["name"], f"{point}: {type(e).__name__}: {e}")
            continue
        if isinstance(out, list):
            results.extend(out)
        elif out is not None:
            results.append(out)
    return results
