// plugins.js — Node 這側的插件點：擴充的登記表（memoryatoms-extension.json，舊名 plugin.json）有寫 "mcp" 就 require 它並呼叫 register(api)。
// 有就掛、沒有就略過；核心程式不寫任何插件的名字。壞掉（登記表格式錯、模組丟例外）核心照開機，stderr 一行 [Guardian:Degraded]。
// 擴充在哪、開著沒：lib/extension-dirs.js（安裝版掃 <程式碼根>/extensions/；插件版讀 installed_plugins.json＋enabledPlugins，
// 開關在 MCP 開機時讀一次）。登記表長相見 hooks/wg_plugins.py 檔頭（MIRROR：name 要和資料夾短名同名）。
// 擴充模組 require 前把 WG_CORE_ROOT 設成核心程式碼根（擴充找核心 lib 先看它，沒有才退回安裝版的相對位置）。
//
// api.registerTool(定義, handler)：登記一個 MCP 工具；定義同 TOOL_DEFINITIONS 的一項，
// handler(args) 回字串，或 { text, isError }（可以是 Promise）。工具名和既有的撞了會丟例外（算這個插件壞掉）。
const fs = require("fs");
const path = require("path");
const { CODE_DIR } = require("./paths");
const { t } = require("./i18n");
const extensionDirs = require("./extension-dirs");

const PLUGINS_DIR = extensionDirs.EXTENSIONS_DIR;   // 安裝版的擴充資料夾（插件版不看它）

function warn(name, why) {
  try { process.stderr.write(t("mcp.plugins.broken", { name, why }) + "\n"); } catch {}
}

function readManifest(name, folder) {
  const file = extensionDirs.registryFile(folder);
  if (!file) return null;   // 沒有登記表的資料夾靜默略過
  let manifest;
  try {
    manifest = JSON.parse(fs.readFileSync(file, "utf-8").replace(/^﻿/, ""));
  } catch (e) {
    warn(name, `${path.basename(file)}: ${e.message}`);
    return null;
  }
  if (!manifest || typeof manifest !== "object" || manifest.name !== name) {
    warn(name, `plugin.json name ${JSON.stringify(manifest && manifest.name)} differs from folder`);
    return null;
  }
  return manifest;
}

function loadPlugins(api) {
  for (const ext of extensionDirs.extensions(warn, PLUGINS_DIR)) {
    if (!ext.enabled) continue;
    const manifest = readManifest(ext.name, ext.folder);
    if (!manifest || !manifest.mcp) continue;
    if (!process.env.WG_CORE_ROOT) process.env.WG_CORE_ROOT = CODE_DIR;
    try {
      require(path.join(ext.folder, manifest.mcp)).register(api);
    } catch (e) {
      warn(ext.name, `mcp ${manifest.mcp}: ${e && e.message || e}`);
    }
  }
}

module.exports = { loadPlugins };
