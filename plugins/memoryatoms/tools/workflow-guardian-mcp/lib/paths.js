// paths.js — 路徑錨與載入器（CLAUDE/WORKFLOW/MEMORY/TOOLS/CONFIG/REGISTRY/VERSION）＋ config/registry/version 讀取。
// 全樹最底層葉模組（零內部相依）。
// i18n.js 反向 require 本檔：這裡惰性載入，避免載入期循環。
const t = (key, values) => require("./i18n").t(key, values);
const fs = require("fs");
const path = require("path");

// CODE_DIR＝程式碼根（本檔所在的樹，tools/workflow-guardian-mcp/lib 往上三層）：spawn 的 py 腳本與 `python -m lib.*` 的 cwd
// 一律用它，worktree 裡跑的就是 worktree 的碼；version.json 是進版控的程式碼版本標記，也看它。
const CODE_DIR = path.resolve(__dirname, "..", "..", "..");
const HOME = require("os").homedir();

// ── 版別與插件設定 ── MIRROR: lib/edition.py（edition、plugin_option、config_layers、layered_config、layered_meta 同一套算法）
// 版別：CLAUDE_PLUGIN_ROOT 等於程式碼根 → plugin；程式碼根有 .claude-plugin/plugin.json → plugin；否則 install。
const PLUGIN = "plugin", INSTALL = "install";
const PLUGIN_NAME = "memoryatoms";
const PLUGIN_IDS = ["memoryatoms@memoryatoms"];   // pluginConfigs 的鍵「插件名@市集名」；沒有再找任何 memoryatoms@*
function samePath(a, b) {   // 展開 8.3 短名與連結再比，Windows 不分大小寫（同 py 的 realpath＋normcase）
  const norm = (p) => {
    let r;
    try { r = fs.realpathSync.native(p); } catch { r = path.resolve(p); }
    r = path.normalize(r);
    return process.platform === "win32" ? r.toLowerCase() : r;
  };
  return norm(a) === norm(b);
}
function edition() {
  const root = (process.env.CLAUDE_PLUGIN_ROOT || "").trim();
  if (root && samePath(root, CODE_DIR)) return PLUGIN;
  try { if (fs.statSync(path.join(CODE_DIR, ".claude-plugin", "plugin.json")).isFile()) return PLUGIN; } catch {}
  return INSTALL;
}
function claudeConfigDir() { return process.env.CLAUDE_CONFIG_DIR || path.join(HOME, ".claude"); }
// settings.json 的 pluginConfigs 裡本插件的 options.<鍵>；只讀使用者層（--settings、受管設定不讀，TODO）。沒有回 ""。
function settingsOption(key) {
  let data;
  try { data = JSON.parse(fs.readFileSync(path.join(claudeConfigDir(), "settings.json"), "utf-8")); } catch { return ""; }
  const configs = data && typeof data === "object" ? data.pluginConfigs : null;
  if (!configs || typeof configs !== "object" || Array.isArray(configs)) return "";
  const ids = PLUGIN_IDS.filter((i) => i in configs).concat(
    Object.keys(configs).filter((i) => i.split("@")[0] === PLUGIN_NAME && !PLUGIN_IDS.includes(i)).sort());
  for (const id of ids) {
    const entry = configs[id];
    const options = entry && typeof entry === "object" ? entry.options : null;
    const value = options && typeof options === "object" ? options[key] : undefined;
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}
// 插件 userConfig 的值：環境變數 CLAUDE_PLUGIN_OPTION_<鍵大寫> → 插件版再看 settings.json；都沒有回 ""。
function pluginOption(key) {
  const env = (process.env["CLAUDE_PLUGIN_OPTION_" + key.toUpperCase()] || "").trim();
  if (env) return env;
  return edition() === PLUGIN ? settingsOption(key) : "";
}
function expandHome(p) {   // 同 py 的 os.path.expanduser：只展開開頭的 ~ 與 ~/、~\
  return p === "~" ? HOME : (p.startsWith("~/") || p.startsWith("~\\") ? path.join(HOME, p.slice(2)) : p);
}
const EDITION = edition();

// CLAUDE_DIR＝使用者資料根（workflow、memory、config、registry）：WG_CLAUDE_DIR → userConfig 的 data_dir → 插件版 ~/.memoryatoms、
// 安裝版 ~/.claude；值去頭尾空白、展開 ~。
// MIRROR: lib/project_root.py:claude_data_dir（兩邊都不 resolve；JS 的 trim 會連 BOM 一起去、Python 的 strip 不會，WG_CLAUDE_DIR 必須給絕對路徑）。
function dataDir() {
  const override = (process.env.WG_CLAUDE_DIR || "").trim();
  if (override) return expandHome(override);
  const option = pluginOption("data_dir");
  if (option) return expandHome(option);
  return path.join(HOME, EDITION === PLUGIN ? ".memoryatoms" : ".claude");
}
const CLAUDE_DIR = dataDir();
const WORKFLOW_DIR = path.join(CLAUDE_DIR, "workflow");
const MEMORY_DIR = path.join(CLAUDE_DIR, "memory");
const TOOLS_DIR = path.join(CODE_DIR, "tools");
const CONFIG_PATH = path.join(WORKFLOW_DIR, "config.json");
const REGISTRY_PATH = path.join(MEMORY_DIR, "project-registry.json");
const VERSION_PATH = path.join(CODE_DIR, "version.json");
function loadVersions() {
  try { return JSON.parse(fs.readFileSync(VERSION_PATH, "utf-8")); }
  catch { return { guardian: "0.0.0", atom_memory: "?" }; }
}
const VERSIONS = loadVersions();

function loadRegistry() {
  try {
    return JSON.parse(fs.readFileSync(REGISTRY_PATH, "utf-8"));
  } catch {
    return { projects: {} };
  }
}

/** Returns [{slug, memDir}] for all registered projects that have .claude/memory/ */
function getRegistryMemDirs() {
  const reg = loadRegistry();
  const results = [];
  for (const [slug, info] of Object.entries(reg.projects || {})) {
    if (!info.root) continue;
    const newMem = path.join(info.root, ".claude", "memory");
    if (fs.existsSync(newMem)) {
      results.push({ slug, memDir: newMem });
    }
  }
  return results;
}

// 共用設定（不含本機覆寫 config.local.json）：插件版出廠兩層墊底、資料根 config.json 蓋上。MIRROR: hooks/wg_core.py:load_config 的分層段。
function loadConfig() {
  return layeredConfig({ withLocal: false });
}

// 分層設定：插件版出廠 config.json → 插件版預設 config.plugin.json → 資料根 config.json → config.local.json，後蓋前（頂層同名段淺合併）；
// 同一個檔只讀一次。出廠兩層只在插件版墊底（安裝版程式碼根＝資料根；資料根另指時照舊只讀資料根）。MIRROR: lib/edition.py:config_layers。
function configLayers({ withLocal = true } = {}) {
  const files = [];
  if (EDITION === PLUGIN) files.push(path.join(CODE_DIR, "workflow", "config.json"), path.join(CODE_DIR, "workflow", "config.plugin.json"));
  files.push(path.join(WORKFLOW_DIR, "config.json"));
  if (withLocal) files.push(path.join(WORKFLOW_DIR, "config.local.json"));
  const out = [];
  for (const f of files) if (!out.some((seen) => samePath(f, seen))) out.push(f);
  return out;
}
function layeredConfig(opts = {}) {
  const config = {};
  for (const f of configLayers(opts)) {
    let layer;
    try { layer = JSON.parse(fs.readFileSync(f, "utf-8").replace(/^\uFEFF/, "")); } catch (e) {   // 去 BOM：同 edition.py 的 utf-8-sig
      if (e.code !== "ENOENT") {
        try { process.stderr.write(t("mcp.paths.local_config_failed", { path: f, error: e.message }) + "\n"); } catch {}
      }
      continue;
    }
    if (!layer || typeof layer !== "object" || Array.isArray(layer)) continue;
    for (const [key, value] of Object.entries(layer)) {
      const cur = config[key];
      const isObj = (v) => v && typeof v === "object" && !Array.isArray(v);
      config[key] = isObj(cur) && isObj(value) ? { ...cur, ...value } : value;
    }
  }
  return config;
}
// 詞表檔：資料根 memory/_meta/<名> 在就用它，插件版才退到程式碼根的。MIRROR: lib/edition.py:layered_meta。
function layeredMeta(name) {
  const own = path.join(MEMORY_DIR, "_meta", name);
  try { if (fs.statSync(own).isFile()) return own; } catch {}
  return EDITION === PLUGIN ? path.join(CODE_DIR, "memory", "_meta", name) : own;
}

// 向量服務埠的唯一算法。MIRROR: lib/project_root.py:vector_service_port —— config.local.json、config.json 的
// vector_search.service_port 有值就用；都沒寫 → 由資料根路徑推（換資料根就換服務），範圍同 py：28000～28999。
const VECTOR_PORT_BASE = 28000;
const VECTOR_PORT_SPAN = 1000;
function servicePortIn(file) {
  try {
    const port = (JSON.parse(fs.readFileSync(file, "utf-8")).vector_search || {}).service_port;
    return Number.isInteger(port) && port > 0 ? port : 0;
  } catch { return 0; }
}
function vectorServicePort() {
  const port = servicePortIn(path.join(WORKFLOW_DIR, "config.local.json")) || servicePortIn(CONFIG_PATH);
  if (port) return port;
  let key = path.normalize(CLAUDE_DIR).replace(/\\/g, "/").replace(/\/+$/, "");
  if (process.platform === "win32") key = key.toLowerCase();   // Windows 路徑不分大小寫
  const digest = require("crypto").createHash("sha1").update(key, "utf8").digest("hex");
  return VECTOR_PORT_BASE + parseInt(digest.slice(0, 8), 16) % VECTOR_PORT_SPAN;
}

// MIRROR: hooks/wg_core.py:v6_enabled — 寫碼守門總開關，出廠開：環境變數 WG_CODING_GUARD=1 開、=0 關（優先；沒設看舊名 WG_V6）；
// 沒設看 config.json 的 coding_guard.enabled，不是 true 再看舊鍵 v6.enabled（相容舊 config.local.json）；
// workflow/config.local.json（不進版控）同名鍵蓋過。config 每次呼叫重讀；環境變數是 MCP 行程啟動時的，改了要重開 session。
const CONFIG_LOCAL_PATH = path.join(WORKFLOW_DIR, "config.local.json");
function v6Enabled() {
  for (const name of ["WG_CODING_GUARD", "WG_V6"]) {
    const env = String(process.env[name] || "").trim();
    if (env === "0" || env === "1") return env === "1";
  }
  let local = {};
  try { local = JSON.parse(fs.readFileSync(CONFIG_LOCAL_PATH, "utf-8")); } catch (e) {
    if (e.code !== "ENOENT") {
      try { process.stderr.write(t("mcp.paths.local_config_failed", { path: CONFIG_LOCAL_PATH, error: e.message }) + "\n"); } catch {}
    }
  }
  const sec = (v) => (v && typeof v === "object" && !Array.isArray(v) ? v : {});   // 只認 JSON 的 true，同 py
  const config = loadConfig() || {};
  const merged = (key) => {   // 同 py merge_local：local 有這個鍵就整段蓋（物件才疊，其餘當空）
    const base = sec(config[key]), over = local && key in local ? local[key] : undefined;
    return over === undefined ? base : (over && typeof over === "object" && !Array.isArray(over) ? { ...base, ...over } : {});
  };
  return merged("coding_guard").enabled === true || merged("v6").enabled === true;
}

// MIRROR: hooks/wg_core.py:load_org_local — 這台機器專屬的組織層狀態（不進版控）；沒檔 → {}，壞檔 → {} 且 stderr 一行。
const ORG_LOCAL_PATH = path.join(WORKFLOW_DIR, "org-memory.local.json");
function loadOrgLocal() {
  let raw;
  try { raw = fs.readFileSync(ORG_LOCAL_PATH, "utf-8"); } catch (e) {
    if (e.code !== "ENOENT") {
      try { process.stderr.write(t("mcp.paths.org_local_failed", { path: ORG_LOCAL_PATH, error: e.message }) + "\n"); } catch {}
    }
    return {};
  }
  try {
    const data = JSON.parse(raw);
    return data && typeof data === "object" && !Array.isArray(data) ? data : {};
  } catch (e) {
    try { process.stderr.write(t("mcp.paths.org_local_failed", { path: ORG_LOCAL_PATH, error: e.message }) + "\n"); } catch {}
    return {};
  }
}

// ── Python 直譯器解析 ──────────────────────────────────────────────────────
// Windows 裸 spawn "python" 會被 PATH 順位上的 Microsoft Store 佔位程式攔走
// （零輸出、exit 9009）→ 下游 JSON 解析炸 "Unexpected end of JSON input" 且
// stderr 無線索。解析順序：WG_PYTHON env → userConfig 的 python → 啟動器（hooks/run.js）留的快取 ${CLAUDE_PLUGIN_DATA}/python.json
// 的 "python" 欄 → Windows 探常見安裝路徑；非 Windows 在 PATH 上先找 python3 再找 python → fail-open 回 "python"。模組載入時解析一次。
const IS_WINDOWS = process.platform === "win32";   // Node 端平台差異的唯一判斷（Python 端見 lib/wg_platform.py）
function onPath(name) {   // PATH 上第一個叫 name 的可執行檔；沒有回 ""
  for (const dir of (process.env.PATH || "").split(path.delimiter)) {
    if (!dir) continue;
    const full = path.join(dir, name);
    try { fs.accessSync(full, fs.constants.X_OK); if (fs.statSync(full).isFile()) return full; } catch {}
  }
  return "";
}
function cachedPython() {   // 啟動器的 Python 快取；沒設 CLAUDE_PLUGIN_DATA、沒檔、壞檔、指的檔不在都回 ""
  const dataRoot = (process.env.CLAUDE_PLUGIN_DATA || "").trim();
  if (!dataRoot) return "";
  try {
    const py = JSON.parse(fs.readFileSync(path.join(dataRoot, "python.json"), "utf-8")).python;
    return typeof py === "string" && py && fs.existsSync(py) ? py : "";
  } catch { return ""; }
}
function resolvePythonExe() {
  const envPy = process.env.WG_PYTHON;
  if (envPy) {
    try { if (fs.existsSync(envPy)) return envPy; } catch {}
  }
  const optPy = expandHome(pluginOption("python"));
  if (optPy) {
    try { if (fs.existsSync(optPy)) return optPy; } catch {}
  }
  const cached = cachedPython();
  if (cached) return cached;
  if (!IS_WINDOWS) return onPath("python3") || onPath("python") || "python";
  const candidates = [];
  const local = process.env.LOCALAPPDATA;
  const versions = ["314", "313", "312", "311", "310"];
  if (local) {
    for (const v of versions) {
      candidates.push(path.join(local, "Programs", "Python", "Python" + v, "python.exe"));
    }
    candidates.push(path.join(local, "Python", "bin", "python.exe"));
  }
  for (const v of versions) {
    candidates.push("C:\\Python" + v + "\\python.exe");
    candidates.push("C:\\Program Files\\Python" + v + "\\python.exe");
  }
  for (const c of candidates) {
    try { if (fs.existsSync(c)) return c; } catch {}
  }
  return "python";
}
const PYTHON_EXE = resolvePythonExe();
// fail-open 必浮訊號：退回裸 "python" 時在 stderr 留一行（MCP stdio 的 stderr 不進協定），
// 否則撞到 Store 佔位程式只會看到下游 "Unexpected end of JSON input"，無從追。
const PYTHON_EXE_FALLBACK = PYTHON_EXE === "python" && IS_WINDOWS;

module.exports = {
  PLUGIN, INSTALL, EDITION, edition, pluginOption, configLayers, layeredConfig, layeredMeta, expandHome,
  CLAUDE_DIR, CODE_DIR, WORKFLOW_DIR, MEMORY_DIR, TOOLS_DIR, CONFIG_PATH, REGISTRY_PATH, VERSION_PATH,
  loadVersions, VERSIONS, loadRegistry, getRegistryMemDirs, loadConfig, loadOrgLocal, v6Enabled, vectorServicePort,
  ORG_LOCAL_PATH, resolvePythonExe, PYTHON_EXE, PYTHON_EXE_FALLBACK, IS_WINDOWS,
};

// 放在 module.exports 之後：i18n 需要本檔的匯出才能決定語言。
if (PYTHON_EXE_FALLBACK) {
  process.stderr.write(t("mcp.paths.python_fallback") + "\n");
}
