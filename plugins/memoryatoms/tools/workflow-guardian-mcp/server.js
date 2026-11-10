/**
 * Workflow Guardian MCP Server — 進入點（entry point）。
 *
 * 職責僅剩：requires/wiring、MCP stdio 轉接（lib/mcp）、插件掛載（lib/plugins）、
 *          boot block、parity export 面。業務邏輯已拆入 lib/*（見 lib/_MAP.md）。
 *
 * MCP stdio server (JSON-RPC). Zero npm deps. Node.js 18+.
 */

const fs = require("fs");
const path = require("path");

const { WORKFLOW_DIR, EDITION, INSTALL } = require("./lib/paths");
const { t } = require("./lib/i18n");
const { onFatal, crashLog } = require("./lib/log");
const { buildAtomContent, renderKnowledgeLines, isBlockKnowledge } = require("./lib/atom-render");
const { registerTool, TOOL_DEFINITIONS } = require("./lib/mcp");   // MCP stdio transport：載入即註冊 process.stdin data handler（buffer 私有其內）
const { loadPlugins } = require("./lib/plugins");

process.on("uncaughtException", (err) => { onFatal("UncaughtException", err); });
process.on("unhandledRejection", (reason) => { onFatal("UnhandledRejection", reason); });
// Actually exit on termination signals. Previously these logged but did NOT
// exit, leaving orphan node processes that could not be shut down normally.
process.on("SIGTERM", () => { crashLog("SIGTERM", "Process received SIGTERM"); process.exit(0); });
process.on("SIGINT", () => { crashLog("SIGINT", "Process received SIGINT"); process.exit(0); });

// Code version = newest mtime across server.js and lib/*.js. The boot record
// lets a hook compare it with what is on disk to judge whether advising a
// Reload Window is pointless (rule RELOAD_ADVICE_ALREADY_LOADED).
function codeMtime() {
  let m = 0;
  try { m = fs.statSync(__filename).mtimeMs; } catch {}
  try {
    const libDir = path.join(__dirname, "lib");
    for (const f of fs.readdirSync(libDir)) {
      if (!f.endsWith(".js")) continue;
      try { m = Math.max(m, fs.statSync(path.join(libDir, f)).mtimeMs); } catch {}
    }
  } catch {}
  return m;
}
// D5 一台只跑一套：插件版遇到安裝版還在跑就讓路——不寫啟動紀錄、一個工具都不註冊，stderr 說一行。
// 安裝版永遠不讓路；被 require（測試）時不判。判斷邏輯和 hooks/run.js 共用 hooks/edition-guard.js。
function editionYields() {
  if (require.main !== module || EDITION === INSTALL) return false;
  try {
    const guard = require(path.join(__dirname, "..", "..", "hooks", "edition-guard.js"));
    if (guard.editionGuard({ event: "McpServerStart" }).active !== false) return false;
    process.stderr.write(guard.yieldMessage() + "\n");
    return true;
  } catch (e) {
    process.stderr.write(t("mcp.server.edition_guard_failed", { error: e && e.message }) + "\n");
    return false;
  }
}
const EDITION_YIELDS = editionYields();
if (!EDITION_YIELDS) {
  require("./lib/boot-record").startBootRecord(path.join(WORKFLOW_DIR, "mcp-boot"), process.pid, codeMtime());
}

// Only boot the plugins when actually run as `node server.js`. Guards against a
// bare require() (parity tests import this module for buildAtomContent) binding
// ports or starting plugin side-effects from a test context.
if (require.main === module) {
  if (EDITION_YIELDS) TOOL_DEFINITIONS.length = 0;   // 核心工具是 lib/mcp 的靜態清單：讓路時就地清空，tools/list 回 0 個
  else loadPlugins({ registerTool });

  // Keep MCP alive
  process.stdin.resume();

  // Orphan prevention (root fix): this process is spawned by exactly one Claude
  // Code client over stdio. When that client exits (session / VS Code closes),
  // the OS closes the pipe's write end and our stdin reaches EOF. We exit so the
  // process dies with its owner instead of lingering as an orphan. This only
  // ever exits THIS process on ITS OWN parent's exit — it never touches another
  // instance, so an active session's MCP is safe (its stdin is still connected
  // to a live parent → no EOF).
  let _parentGone = false;
  const exitOnParentGone = (why) => {
    if (_parentGone) return;
    _parentGone = true;
    try { process.stderr.write(`[workflow-guardian] stdin ${why}; parent gone, exiting (orphan prevention).\n`); } catch {}
    process.exit(0);
  };
  process.stdin.on("end", () => exitOnParentGone("end"));
  process.stdin.on("close", () => exitOnParentGone("close"));
}

// Test/tooling surface: pure content builders (no side effects). Safe to require
// for parity tests; production boots via `node server.js` (require.main === module)
// and is unaffected. See lib/verify/verify_atom_io_equivalence.py:test_13.
module.exports = { buildAtomContent, renderKnowledgeLines, isBlockKnowledge };
