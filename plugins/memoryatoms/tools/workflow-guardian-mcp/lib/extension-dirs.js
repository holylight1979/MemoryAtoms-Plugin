// extension-dirs.js — 擴充（memoryatoms-<名>）裝在哪、開著沒：lib/plugins.js（MCP 掛點）與 lib/i18n.js（ext.* 表）共用。
// MIRROR: lib/extension_dirs.py（找法、開關、登記表檔名、遷移提示要同步；說明以那份檔頭為準）。
//   安裝版：<程式碼根>/extensions/<名>/；有 .claude-plugin/plugin.json 的要 enabledPlugins 裡 <名>@memory-atoms 或
//           memoryatoms-<名>@memory-atoms 為 true，沒有的有檔就掛。
//   插件版：<CLAUDE_CONFIG_DIR 或 ~/.claude>/plugins/installed_plugins.json 的 memoryatoms-* 條目（installPath；projectPath 等於目前專案的優先），
//           那個「插件@市集」在 enabledPlugins 為 true 才掛；開了卻找不到 → warn 一行 [Guardian:Degraded]。
//   登記表 memoryatoms-extension.json；舊名 plugin.json 照讀，stderr 一行遷移提示（不算降級）。
const fs = require("fs");
const os = require("os");
const path = require("path");
const { CODE_DIR, EDITION, PLUGIN } = require("./paths");

const PREFIX = "memoryatoms-";
const REGISTRY = "memoryatoms-extension.json";
const OLD_REGISTRY = "plugin.json";
const LOCAL_MARKETPLACE = "memory-atoms";   // MIRROR: tools/install.py
const EXTENSIONS_DIR = path.join(CODE_DIR, "extensions");

function configDir() { return process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), ".claude"); }   // MIRROR: lib/project_root.py claude_config_dir
function readJson(file) { return JSON.parse(fs.readFileSync(file, "utf-8").replace(/^﻿/, "")); }
function isFile(p) { try { return fs.statSync(p).isFile(); } catch { return false; } }
function isDir(p) { try { return fs.statSync(p).isDirectory(); } catch { return false; } }

const migrateNoted = new Set();
function registryFile(folder) {
  const neu = path.join(folder, REGISTRY);
  if (isFile(neu)) return neu;
  const old = path.join(folder, OLD_REGISTRY);
  if (!isFile(old)) return null;
  if (!migrateNoted.has(folder)) {
    migrateNoted.add(folder);
    let line = `[Guardian:Migrate] extension ${path.basename(folder)}: rename ${OLD_REGISTRY} to ${REGISTRY} (the old name still loads)`;
    try { line = require("./i18n").t("mcp.extension_dirs.old_registry", { name: path.basename(folder), old: OLD_REGISTRY, new: REGISTRY }); } catch {}
    try { process.stderr.write(line + "\n"); } catch {}
  }
  return old;
}

// 使用者層 → 專案層 → 專案本機層，後蓋前，逐鍵合併。
function enabledPlugins(warn) {
  const project = path.join(process.env.CLAUDE_PROJECT_DIR || ".", ".claude");
  const merged = {};
  for (const file of [path.join(configDir(), "settings.json"), path.join(project, "settings.json"), path.join(project, "settings.local.json")]) {
    let text;
    try { text = fs.readFileSync(file, "utf-8"); } catch { continue; }   // 沒有檔＝沒話說
    try { Object.assign(merged, JSON.parse(text.replace(/^﻿/, "")).enabledPlugins); } catch (e) { warn(file, `enabledPlugins: ${e.message}`); }
  }
  return merged;
}

function samePath(a, b) {
  const n = (p) => { const r = path.normalize(path.resolve(p)); return process.platform === "win32" ? r.toLowerCase() : r; };
  return n(a) === n(b);
}

function pickInstall(entries) {
  if (!Array.isArray(entries)) return null;
  const project = process.env.CLAUDE_PROJECT_DIR || process.cwd();
  const live = entries.filter((e) => e && typeof e === "object" && e.installPath && isDir(String(e.installPath)));
  const mine = live.find((e) => e.projectPath && samePath(String(e.projectPath), project));
  return (mine || live[0]) ? String((mine || live[0]).installPath) : null;
}

function installed(warn) {
  const file = path.join(configDir(), "plugins", "installed_plugins.json");
  let plugins;
  try {
    if (!isFile(file)) return {};
    plugins = readJson(file).plugins || {};
  } catch (e) {
    warn(file, `installed_plugins.json: ${e.message}`);
    return {};
  }
  const out = {};
  for (const id of Object.keys(plugins).sort()) {
    if (!id.startsWith(PREFIX)) continue;
    const where = pickInstall(plugins[id]);
    if (where) out[id] = where;
  }
  return out;
}

function fromInstallEdition(root, warn) {
  let names;
  try {
    names = fs.readdirSync(root, { withFileTypes: true }).filter((d) => d.isDirectory()).map((d) => d.name).sort();
  } catch {
    return [];   // 沒有 extensions/＝純核心
  }
  const enabled = names.length ? enabledPlugins(warn) : {};
  return names.map((name) => {
    const folder = path.join(root, name);
    const native = isFile(path.join(folder, ".claude-plugin", "plugin.json"));
    const on = !native || [name, PREFIX + name].some((n) => enabled[`${n}@${LOCAL_MARKETPLACE}`] === true);
    return { name, folder, enabled: on };
  });
}

function fromPluginEdition(warn) {
  const enabled = enabledPlugins(warn);
  const found = installed(warn);
  const out = Object.entries(found).map(([id, folder]) => ({ name: id.split("@")[0].slice(PREFIX.length), folder, enabled: enabled[id] === true }));
  for (const id of Object.keys(enabled).sort()) {
    if (enabled[id] === true && id.startsWith(PREFIX) && !(id in found)) {
      warn(id.split("@")[0], "enabled in settings but not found in plugins/installed_plugins.json (or its installPath is gone)");
    }
  }
  return out.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
}

// 所有找得到的擴充（含沒開的），照短名排序；同名只留第一個。
function extensions(warn = () => {}, root = EXTENSIONS_DIR) {
  const found = EDITION === PLUGIN ? fromPluginEdition(warn) : fromInstallEdition(root, warn);
  const seen = new Set();
  return found.filter((e) => (seen.has(e.name) ? false : (seen.add(e.name), true)));
}

let folders = null;
// 擴充短名 → 資料夾（給 ext.<名>.* 找語言表）；找不到回安裝版位置，讓讀表那邊照常報缺表。
function folderOf(name) {
  if (folders === null) folders = Object.fromEntries(extensions().map((e) => [e.name, e.folder]));
  return folders[name] || path.join(EXTENSIONS_DIR, name);
}

module.exports = { PREFIX, REGISTRY, OLD_REGISTRY, EXTENSIONS_DIR, extensions, registryFile, folderOf };
