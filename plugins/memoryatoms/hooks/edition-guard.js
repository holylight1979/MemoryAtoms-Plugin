'use strict';
// 回答「這一次插件版的 hook／MCP 要不要真的跑」：安裝版還在跑時插件版讓路（D5：一台只跑一套；
// 官方說插件的 hook 和 settings 裡同一支 hook 不去重，兩套同開＝重複注入、MCP 工具兩套）。
// 「安裝版正在跑」＝下面任一條成立：
//   1. Claude 設定目錄（CLAUDE_CONFIG_DIR，沒有就 ~/.claude）的 settings.json 有 hook 指令提到
//      workflow-guardian.py，而且不是指向本插件（不含 CLAUDE_PLUGIN_ROOT、也不含本插件資料夾的路徑）；
//   2. 使用者層 MCP 註冊（CLAUDE_CONFIG_DIR 或家目錄本身底下的 .claude.json，同 lib/project_root.claude_global_config）
//      的 mcpServers 有 workflow-guardian。
// 只用 Node 標準庫、只讀檔；hooks/run.js 每個 hook 都叫，server.js 開機叫一次。判不了回 active:true（照常跑）、why 寫原因。
// MIRROR: tools/migrate-to-plugin.py 用同一套條件找要拿掉的東西（WG_HOOK_SCRIPT、MCP_NAME）。

const fs = require('fs');
const os = require('os');
const path = require('path');

const WG_HOOK_SCRIPT = 'workflow-guardian.py';
const MCP_NAME = 'workflow-guardian';

function claudeConfigDir(env) {
  return env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), '.claude');
}

function claudeGlobalConfig(env) {
  return path.join(env.CLAUDE_CONFIG_DIR || os.homedir(), '.claude.json');
}

// 比對用：斜線統一、Windows 不分大小寫；8.3 短名與連結展開成長名。
function normPath(p) {
  let r = String(p || '');
  try { r = fs.realpathSync.native(r); } catch { r = path.resolve(r); }
  r = r.replace(/\\/g, '/').replace(/\/+$/, '');
  return process.platform === 'win32' ? r.toLowerCase() : r;
}

function normText(s) {
  const r = String(s || '').replace(/\\\\/g, '/').replace(/\\/g, '/');
  return process.platform === 'win32' ? r.toLowerCase() : r;
}

// 檔案不在 → null；讀到但不是 JSON → 丟出（由呼叫端轉成 error:<類型>）。
function readJson(file, needle) {
  let raw;
  try { raw = fs.readFileSync(file, 'utf8'); } catch (e) {
    if (e && e.code === 'ENOENT') return null;
    throw e;
  }
  if (needle && !raw.includes(needle)) return {}; // 連字都沒有就不必解析（.claude.json 常有幾百 KB）
  return JSON.parse(raw.replace(/^﻿/, ''));
}

// settings.json 裡所有 hook 指令字串（command 形式與 exec 形式的 args 都算）。
function hookCommands(settings) {
  const out = [];
  const hooks = settings && typeof settings === 'object' ? settings.hooks : null;
  if (!hooks || typeof hooks !== 'object') return out;
  for (const groups of Object.values(hooks)) {
    if (!Array.isArray(groups)) continue;
    for (const g of groups) {
      for (const h of (g && Array.isArray(g.hooks) ? g.hooks : [])) {
        if (!h || typeof h !== 'object') continue;
        const parts = [h.command, ...(Array.isArray(h.args) ? h.args : [])].filter((x) => typeof x === 'string');
        if (parts.length) out.push(parts.join(' '));
      }
    }
  }
  return out;
}

function installHookPresent(env, pluginRoot) {
  const settings = readJson(path.join(claudeConfigDir(env), 'settings.json'), WG_HOOK_SCRIPT);
  if (!settings) return false;
  const mine = normPath(pluginRoot);
  return hookCommands(settings).some((cmd) => {
    const c = normText(cmd);
    if (!c.includes(WG_HOOK_SCRIPT)) return false;
    if (c.includes('claude_plugin_root')) return false;
    return !(mine && c.includes(mine));
  });
}

function installMcpPresent(env) {
  const cfg = readJson(claudeGlobalConfig(env), `"${MCP_NAME}"`);
  const servers = cfg && typeof cfg === 'object' ? cfg.mcpServers : null;
  return !!(servers && typeof servers === 'object' && Object.prototype.hasOwnProperty.call(servers, MCP_NAME));
}

// SPEC 第 4 節的介面。event 目前不影響判斷（留給呼叫端決定要不要說話）。
// pluginRoot：本插件資料夾；預設 CLAUDE_PLUGIN_ROOT，沒有就本檔的上一層。
function editionGuard({ event, env = process.env, pluginRoot } = {}) { // eslint-disable-line no-unused-vars
  let err = '';
  const probe = (fn) => {
    try { return fn(); } catch (e) { err = err || `error:${(e && (e.code || e.name)) || 'unknown'}`; return false; }
  };
  const e = env || {};
  const root = pluginRoot || e.CLAUDE_PLUGIN_ROOT || path.dirname(__dirname);
  // 兩條各自判：一條讀壞不蓋掉另一條；任一條成立就讓路。
  if (probe(() => installHookPresent(e, root)) || probe(() => installMcpPresent(e))) {
    return { active: false, why: 'install-present' };
  }
  return { active: true, why: err || 'ok' };
}

// 給人看的一行（字走語言表）；語言表載不起來就退回英文原句，不丟例外。
function yieldMessage() {
  try {
    return require(path.join(__dirname, '..', 'tools', 'workflow-guardian-mcp', 'lib', 'i18n.js')).t('hook.edition_guard.yield');
  } catch {
    return '[MemoryAtoms] The installed edition is active on this machine, so the plugin edition stays off. To switch, run /memoryatoms:migrate-to-plugin.';
  }
}

// 相容出口：現行 hooks/run.js 叫 check({pluginRoot, event})，讓路時把 why 寫到 stdout。
// 這裡把 why 清空（stdout 不給 Claude 任何東西），SessionStart 的 workflow-guardian.py 那一條自己用 stderr 說一行。
// run.js 改叫 editionGuard 之後（片段見 W10 回報）這支就不再被用到。
function check(ctx = {}) {
  const g = editionGuard({ event: ctx.event, pluginRoot: ctx.pluginRoot });
  if (g.active) return g;
  const script = path.basename(String(process.argv[2] || '')).toLowerCase();
  if (ctx.event === 'SessionStart' && script === WG_HOOK_SCRIPT) {
    try { process.stderr.write(yieldMessage() + '\n'); } catch { /* stderr 關了 */ }
  }
  return { active: false, why: '' };
}

module.exports = { editionGuard, yieldMessage, check, WG_HOOK_SCRIPT, MCP_NAME };
