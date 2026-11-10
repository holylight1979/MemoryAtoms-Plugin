// i18n.js — 給人看的提示走語言表（lib/i18n/zh-TW.json、en.json）：程式只留鍵。
// 插件的鍵寫成 ext.<插件資料夾>.<短名>，查插件自己的 <擴充資料夾>/i18n/ 那兩張表（安裝版 extensions/<名>/，插件版是 memoryatoms-<名>
// 裝的位置，lib/extension-dirs.js folderOf）；核心不認得任何插件，看鍵就知道去哪找。
// MIRROR: lib/i18n/__init__.py（語言決定順序、缺鍵回鍵名＋stderr 一行、{name} 代換規則、插件鍵查哪張表要同步）。
const fs = require("fs");
const path = require("path");
const { CODE_DIR, WORKFLOW_DIR, loadConfig, pluginOption, EDITION, PLUGIN } = require("./paths");

const DEFAULT_LANGUAGE = "en";
const TABLE_DIR = path.join(CODE_DIR, "lib", "i18n");
const EXTENSIONS_DIR = path.join(CODE_DIR, "extensions");   // 插件自帶的表：extensions/<插件資料夾>/i18n/<語言>.json
const SPLIT_PARTS = ["hook", "cli", "mcp"];   // 插件包把核心表依鍵前綴拆成 <語言>.<部位>.json（tools/build-plugin.py）；原始碼 repo 是單檔
const tables = {};   // 表名（en.json、extensions/<插件資料夾>/i18n/en.json）→ 內容
const warned = new Set();
let configLanguage = null;

function warnOnce(message) {
  if (warned.has(message)) return;
  warned.add(message);
  process.stderr.write(`[i18n] ${message}\n`);
}

function localLanguage() {
  try {
    const local = JSON.parse(fs.readFileSync(path.join(WORKFLOW_DIR, "config.local.json"), "utf-8"));
    return (local && typeof local === "object" && local.language) || "";
  } catch {
    return "";
  }
}

// 語言：WG_LANG → userConfig 的 language（CLAUDE_PLUGIN_OPTION_LANGUAGE，插件版再看 settings.json）→ 資料根 config.local.json、config.json → en。
let optionLanguage = null;
function currentLanguage() {
  const fromEnv = (process.env.WG_LANG || "").trim();
  if (fromEnv) return fromEnv;
  if (optionLanguage === null) optionLanguage = pluginOption("language");
  if (optionLanguage) return optionLanguage;
  if (configLanguage === null) configLanguage = String(localLanguage() || (loadConfig() || {}).language || DEFAULT_LANGUAGE);
  return configLanguage;
}

// 這個鍵查哪張表 → [表名, 檔]：ext.<插件資料夾>.<短名> 查那個插件自帶的表，其他鍵查核心表。
function tableOf(key, language) {
  const parts = key.split(".");
  if (parts.length >= 3 && parts[0] === "ext") {
    const folder = EDITION === PLUGIN ? require("./extension-dirs").folderOf(parts[1]) : path.join(EXTENSIONS_DIR, parts[1]);
    return [`extensions/${parts[1]}/i18n/${language}.json`, path.join(folder, "i18n", `${language}.json`)];
  }
  return [`${language}.json`, path.join(TABLE_DIR, `${language}.json`)];
}

// 核心表有單檔 <語言>.json 就讀它；沒有就合併插件包裡拆開的三檔 <語言>.hook／cli／mcp.json（缺一檔整張算讀不到）。
function readTable(file) {
  if (fs.existsSync(file) || path.dirname(file) !== TABLE_DIR) return JSON.parse(fs.readFileSync(file, "utf-8"));
  const stem = path.basename(file, ".json");
  const merged = {};
  for (const part of SPLIT_PARTS) {
    Object.assign(merged, JSON.parse(fs.readFileSync(path.join(TABLE_DIR, `${stem}.${part}.json`), "utf-8")));
  }
  return merged;
}

function tableFor(name, file) {
  if (!(name in tables)) {
    try {
      tables[name] = readTable(file);
    } catch (e) {
      warnOnce(`language table ${name} unavailable: ${e.message}`);
      tables[name] = {};
    }
  }
  return tables[name];
}

/** 取目前語言的文字；{name} 換成 values 的值，values 沒給的 {name} 原樣留著。 */
function t(key, values = {}) {
  const language = currentLanguage();
  const [name, file] = tableOf(key, language);
  const text = tableFor(name, file)[key];
  if (text === undefined) {
    warnOnce(`missing key ${key} in ${name}`);
    return key;
  }
  return text.replace(/\{(\w+)\}/g, (whole, name) => (name in values ? String(values[name]) : whole));
}

module.exports = { t, currentLanguage };
