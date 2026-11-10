// index.js — 儀表板插件的 Node 面：MCP 行程裡開一個本機 HTTP 服務（dashboard、world-chat、收尾報告 HUD），
// 佔埠與「釋放埠」握手也在這裡。核心只認 plugin.json 寫的本檔、呼叫 register；沒有本插件 MCP 照開。
// 零 npm 相依。
const fs = require("fs");
const path = require("path");
const http = require("http");
const CORE_LIB = path.join(process.env.WG_CORE_ROOT || path.resolve(__dirname, "..", "..", ".."), "tools", "workflow-guardian-mcp", "lib");   // 核心：插件版由 MCP 載入器給 WG_CORE_ROOT，安裝版是本檔往上三層
const { WORKFLOW_DIR, VERSIONS, loadConfig } = require(path.join(CORE_LIB, "paths"));
const { crashLog } = require(path.join(CORE_LIB, "log"));
const { resolveSessionId, readState, applyStateChanges, deleteState, listAllSessions } = require("./state");
const dashboardHtml = require("./dashboard-html");
const worldChat = require("./world-chat");   // 腦內世界生物 LLM 對話代理（同進程模組）
const hud = require("./hud-api");              // Anti-Evasion HUD 唯讀 API + heartbeat
const aecHudHtml = require("./aec-hud-html");  // Anti-Evasion HUD 頁模板
const {
  jsonRes,
  apiHealth, apiTestRunStart, apiTestRunStatus,
  apiWorldCommandPost, apiWorldCommandsGet, apiWorldResultPost, apiWorldSnapshot, apiWorldDev,
  apiHealAll, apiHealReview, apiHealJobStatus, apiHealStart,
  apiVectorStatus, apiOllamaBackendsStatus, apiKnowledgeQueue,
  apiAtoms, apiProjects, apiSkills, apiMcpServers,
} = require("./http-api");

// WG_DASHBOARD_PORT env override (testing / running a second isolated instance)
// takes precedence over config; falls back to config then the 3848 default.
const DASHBOARD_PORT = Number(process.env.WG_DASHBOARD_PORT) || loadConfig().dashboard_port || 3848;

// Code version = newest mtime across the core MCP code and this plugin. A newer
// instance uses it (via /api/whoami · /api/relinquish) to recognize us as stale
// code and ask us to hand off the port. See the port-binding section.
function codeMtime() {
  let m = 0;
  try { m = fs.statSync(path.join(CORE_LIB, "..", "server.js")).mtimeMs; } catch {}
  for (const dir of [CORE_LIB, __dirname]) {
    try {
      for (const f of fs.readdirSync(dir)) {
        if (!f.endsWith(".js")) continue;
        try { m = Math.max(m, fs.statSync(path.join(dir, f)).mtimeMs); } catch {}
      }
    } catch {}
  }
  return m;
}
const SELF_MTIME_AT_BOOT = codeMtime();

function route(req, res) {
  const url = new URL(req.url, `http://localhost:${DASHBOARD_PORT}`);
  const pathname = url.pathname;

  // CORS for local dev
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");

  if (req.method === "OPTIONS") {
    res.writeHead(204);
    return res.end();
  }

  // Dashboard
  if (pathname === "/" && req.method === "GET") {
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    return res.end(dashboardHtml.render(VERSIONS));
  }

  // API: identify this instance — pid, the server.js file it loaded, and that
  // file's mtime at our boot. Lets a newer instance judge whether the current
  // port holder is stale code, and lets a human verify "is new code live?".
  if (pathname === "/api/whoami" && req.method === "GET") {
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({ pid: process.pid, file: __filename, mtime: SELF_MTIME_AT_BOOT }));
  }

  // API: cooperative port hand-off (self-heal). A newer instance running the SAME
  // plugin file (requesterFile === our __filename) with fresher code (requesterMtime
  // > our boot-time mtime) asks us to release the port. We ACK, then close OUR HTTP
  // side so it can rebind. No process ever kills another, and nothing but our own
  // listening socket goes away. A non-guardian process has no such
  // route (404) so nothing outside our own code is ever affected. Localhost-only,
  // same trust model as /api/sessions/:id/signal.
  if (pathname === "/api/relinquish" && req.method === "POST") {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      let reqMtime = NaN, reqFile = "";
      try {
        const j = JSON.parse(body || "{}");
        reqMtime = Number(j.requesterMtime);
        reqFile = String(j.requesterFile || "");
      } catch {}
      const sameFile = !!reqFile &&
        path.resolve(reqFile).toLowerCase() === path.resolve(__filename).toLowerCase();
      const newer = Number.isFinite(reqMtime) && reqMtime > SELF_MTIME_AT_BOOT;
      const relinquishing = sameFile && newer;
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ relinquishing, pid: process.pid, mtime: SELF_MTIME_AT_BOOT }));
      if (relinquishing) {
        process.stderr.write(`[workflow-guardian] Relinquishing port ${DASHBOARD_PORT}: my code (mtime ${SELF_MTIME_AT_BOOT}) is older than requester (${reqMtime}); closing the dashboard, MCP tools keep running.\n`);
        // Flush the ACK, then close only our HTTP side: this is a plugin, handing off the
        // port must not take the core MCP tools of this session down with it.
        setTimeout(() => {
          try { httpServer.close(); httpServer.closeAllConnections(); } catch {}
          startHeartbeat();   // if the new holder dies, a surviving instance reclaims the port
        }, 200);
      }
    });
    return;
  }

  // API: list sessions
  if (pathname === "/api/sessions" && req.method === "GET") {
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify(listAllSessions()));
  }

  // API: get/delete single session
  const sessionMatch = pathname.match(/^\/api\/sessions\/([^/]+)$/);
  if (sessionMatch) {
    const sid = resolveSessionId(sessionMatch[1]) || sessionMatch[1];
    if (req.method === "GET") {
      const state = readState(sid);
      if (!state) {
        res.writeHead(404, { "Content-Type": "application/json" });
        return res.end(JSON.stringify({ error: "not found" }));
      }
      res.writeHead(200, { "Content-Type": "application/json" });
      return res.end(JSON.stringify(state));
    }
    if (req.method === "DELETE") {
      const ok = deleteState(sid);
      res.writeHead(ok ? 200 : 404, { "Content-Type": "application/json" });
      return res.end(JSON.stringify({ ok, deleted: `state-${sid}.json` }));
    }
  }

  // API: send signal
  const signalMatch = pathname.match(/^\/api\/sessions\/([^/]+)\/signal$/);
  if (signalMatch && req.method === "POST") {
    const sid = resolveSessionId(signalMatch[1]) || signalMatch[1];
    let body = "";
    req.on("data", (chunk) => (body += chunk));
    req.on("end", async () => {
      let signal;
      try {
        ({ signal } = JSON.parse(body));
      } catch {
        res.writeHead(400, { "Content-Type": "application/json" });
        return res.end(JSON.stringify({ error: "invalid body" }));
      }
      if (!readState(sid)) {
        res.writeHead(404, { "Content-Type": "application/json" });
        return res.end(JSON.stringify({ error: "not found" }));
      }
      // 只送這個訊號要改的鍵；讀改寫在 hook 同一把 state 鎖裡做（applyStateChanges），不整份覆蓋 hook 剛寫的那份
      const SIGNAL_CHANGES = {
        sync_started: () => ({ phase: "syncing" }),
        sync_completed: () => ({ phase: "done", sync_pending: false, knowledge_queue: [], modified_files: [],
                                 ended_at: new Date().toISOString() }),
        reset: () => ({ phase: "working", sync_pending: false, stop_blocked_count: 0, remind_count: 0, muted: false }),
        mute: () => ({ muted: true }),
      };
      // 只認自有屬性（`__proto__` 這類繼承來的名字不算），不認得的 signal 不改任何鍵（同改前：照讀照回）
      const changes = Object.prototype.hasOwnProperty.call(SIGNAL_CHANGES, signal) ? SIGNAL_CHANGES[signal]() : {};
      let r;
      try {
        r = await applyStateChanges(sid, changes);
      } catch (e) {
        r = { ok: false, error: e && e.message || String(e) };
      }
      if (!r.ok) {
        res.writeHead(500, { "Content-Type": "application/json" });
        return res.end(JSON.stringify({ ok: false, outcome: r.outcome, error: r.error }));
      }
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ ok: true, outcome: r.outcome, state: r.state || readState(sid) }));
    });
    return;
  }

  // v2.1 API routes
  if (pathname === "/api/health" && req.method === "GET") {
    const force = url.searchParams.get("force") === "1";
    return apiHealth(req, res, force);
  }
  if (pathname === "/api/test-run" && req.method === "POST") {
    return apiTestRunStart(req, res);
  }
  const testJobMatch = pathname.match(/^\/api\/test-run\/([^/]+)$/);
  if (testJobMatch && req.method === "GET") {
    return apiTestRunStatus(req, res, testJobMatch[1]);
  }
  // ── World Command Bus 路由 ──
  if (pathname === "/api/world-command" && req.method === "POST") return apiWorldCommandPost(req, res);
  if (pathname === "/api/world-commands" && req.method === "GET") return apiWorldCommandsGet(req, res, url.searchParams.get("since"));
  if (pathname === "/api/world-result" && req.method === "POST") return apiWorldResultPost(req, res);
  if (pathname === "/api/world-snapshot") return apiWorldSnapshot(req, res);
  if (pathname === "/api/world-dev") return apiWorldDev(req, res);
  // ── 記憶自癒路由（先比對固定路徑，再 heal/(.+) 否則被吃掉）──
  if (pathname === "/api/heal-all" && req.method === "POST") return apiHealAll(req, res);
  if (pathname === "/api/heal-review" && req.method === "GET") return apiHealReview(req, res);
  const healJobMatch = pathname.match(/^\/api\/heal-job\/([^/]+)$/);
  if (healJobMatch && req.method === "GET") return apiHealJobStatus(req, res, healJobMatch[1]);
  const healMatch = pathname.match(/^\/api\/heal\/(.+)$/);
  if (healMatch && req.method === "POST") return apiHealStart(req, res, decodeURIComponent(healMatch[1]), url.searchParams.get("auto") === "1");
  if (pathname === "/api/vector-status" && req.method === "GET") {
    return apiVectorStatus(req, res);
  }
  if (pathname === "/api/ollama-backends-status" && req.method === "GET") {
    return apiOllamaBackendsStatus(req, res);
  }
  if (pathname === "/api/creature-chat" && req.method === "POST") {
    return worldChat.handleCreatureChat(req, res, { loadConfig, jsonRes, WORKFLOW_DIR, fs, path });
  }
  if (pathname === "/api/knowledge-queue" && req.method === "GET") {
    return apiKnowledgeQueue(req, res);
  }
  if (pathname === "/api/atoms" && req.method === "GET") {
    return apiAtoms(req, res);
  }
  if (pathname === "/api/projects" && req.method === "GET") {
    return apiProjects(req, res);
  }
  if (pathname === "/api/skills" && req.method === "GET") {
    return apiSkills(req, res);
  }
  if (pathname === "/api/mcp-servers" && req.method === "GET") {
    return apiMcpServers(req, res);
  }

  // ── Hot Cache status ──
  if (pathname === "/api/hot-cache" && req.method === "GET") {
    const cachePath = path.join(WORKFLOW_DIR, "hot_cache.json");
    try {
      const raw = fs.readFileSync(cachePath, "utf-8");
      const data = JSON.parse(raw);
      data.age_seconds = Math.round(Date.now() / 1000 - (data.timestamp || 0));
      res.writeHead(200, { "Content-Type": "application/json" });
      return res.end(JSON.stringify(data));
    } catch {
      res.writeHead(200, { "Content-Type": "application/json" });
      return res.end(JSON.stringify({ empty: true }));
    }
  }

  // ── Vector Ready indicator ──
  if (pathname === "/api/vector-ready" && req.method === "GET") {
    const flagPath = path.join(WORKFLOW_DIR, "vector_ready.flag");
    const ready = fs.existsSync(flagPath);
    res.writeHead(200, { "Content-Type": "application/json" });
    return res.end(JSON.stringify({ ready }));
  }

  // ── Anti-Evasion HUD 路由（唯讀；港口持有者供頁 + glob disk 上 Python 落的 report 檔）──
  if (pathname === "/aec/hud" && req.method === "GET") {
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    return res.end(aecHudHtml.render());
  }
  if (pathname === "/api/aec/reports" && req.method === "GET") {
    return hud.apiAecReports(req, res, url.searchParams.get("since"));
  }
  if (pathname === "/api/aec/tempfiles-all" && req.method === "GET") {
    return hud.apiAecTempfilesAll(req, res);   // 全機殘留（全部帳本、依 session 分組）
  }
  const aecTempMatch = pathname.match(/^\/api\/aec\/tempfiles\/([^/]+)$/);
  if (aecTempMatch && req.method === "GET") {
    return hud.apiAecTempfiles(req, res, aecTempMatch[1]);
  }
  const aecReportMatch = pathname.match(/^\/api\/aec\/report\/([^/]+)\/(\d+)$/);
  if (aecReportMatch && req.method === "GET") {
    return hud.apiAecReport(req, res, aecReportMatch[1], aecReportMatch[2]);
  }
  if (pathname === "/api/aec/beat" && req.method === "GET") {
    return hud.apiAecBeat(req, res);
  }
  if (pathname === "/api/aec/beat-status" && req.method === "GET") {
    return hud.apiAecBeatStatus(req, res);
  }
  if (pathname === "/api/aec/stream" && req.method === "GET") {
    return hud.apiAecStream(req, res);   // HUD 頁 SSE 常駐連線（窗開著的證據）
  }
  if (pathname === "/api/aec/decision" && req.method === "POST") {
    return hud.apiAecDecisionPost(req, res);   // HUD (d) 保留/刪除鈕 → 決策落磁碟
  }

  res.writeHead(404);
  res.end("Not found");
}

const httpServer = http.createServer((req, res) => {
  try {
    route(req, res);
  } catch (e) {
    crashLog("DashboardRoute", e);
    try { res.writeHead(500); res.end("Internal error"); } catch {}
  }
});

// ─── Dashboard port binding with recovery heartbeat + stale-orphan reclaim ───
// When multiple Claude Code instances exist, only one binds the dashboard port.
// If that instance dies, a surviving instance reclaims the port via heartbeat.
//
// Self-heal: an old session can hold the port while serving STALE code, so edited
// routes never go live (POST /api/<new route> → 404). When we find the port held, we
// ask the holder to hand off via POST /api/relinquish: a holder running OUR plugin
// file with OLDER code (its boot-time mtime < this file's current mtime) closes its
// HTTP side and we rebind; a peer on current code, or any non-guardian process
// (no such route → not-ok/404), keeps the port and we yield. We never kill another
// process, and only our own code exposes the relinquish contract. Pure
// Node http (no shell / external process spawn), so nothing for AV to flag and it
// works on any platform.
const HEARTBEAT_INTERVAL_MS = 15000;
let dashboardHeartbeat = null;
let _reclaiming = false;

/** If port DASHBOARD_PORT is held by our own stale (old-code) instance, ask it to
 *  relinquish (it closes its HTTP side) and rebind. Otherwise yield. No-op while busy. */
function reclaimStaleOrphan() {
  if (_reclaiming || httpServer.listening) return;
  _reclaiming = true;
  const done = () => { _reclaiming = false; };

  const currentMtime = codeMtime();
  if (!currentMtime) return done();
  // Only an instance that itself booted on the CURRENT file may ask. Without this,
  // editing the code under live sessions makes every old instance (fresh stat >
  // its own boot mtime) ask the holder to release, the next old binder gets asked
  // in turn, and the dashboard keeps bouncing between stale instances.
  if (currentMtime !== SELF_MTIME_AT_BOOT) return done();

  const payload = JSON.stringify({ requesterMtime: currentMtime, requesterFile: __filename });
  const req = http.request(
    { hostname: "127.0.0.1", port: DASHBOARD_PORT, path: "/api/relinquish", method: "POST",
      headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(payload) },
      timeout: 1500 },
    (res) => {
      let body = "";
      res.setEncoding("utf-8");
      res.on("data", (c) => { body += c; });
      res.on("end", () => {
        let out;
        try { out = JSON.parse(body); } catch { return done(); }
        // Only a stale holder of OUR code answers relinquishing:true; peers on
        // current code answer false, non-guardians never reach here → yield.
        if (res.statusCode !== 200 || !out || !out.relinquishing) return done();
        process.stderr.write(`[workflow-guardian] Stale holder pid=${out.pid} relinquishing port ${DASHBOARD_PORT}; rebinding.\n`);
        // Holder is closing itself; give the socket a moment to free, then rebind.
        setTimeout(() => { _reclaiming = false; tryBindDashboard(); }, 700);
      });
    }
  );
  req.on("error", () => done());   // no relinquish route / unreachable → yield
  req.on("timeout", () => { req.destroy(); done(); });
  req.write(payload);
  req.end();
}

function tryBindDashboard() {
  if (httpServer.listening) return;

  const probe = http.request(
    { hostname: "127.0.0.1", port: DASHBOARD_PORT, path: "/", method: "HEAD", timeout: 500 },
    () => {
      // Port occupied by an HTTP server. If it is our own stale orphan, ask it to
      // hand off; otherwise reclaimStaleOrphan() yields and the heartbeat waits.
      probe.destroy();
      reclaimStaleOrphan();
    }
  );

  probe.on("error", () => {
    // Connection refused → port is free, attempt to bind
    if (httpServer.listening) return;
    httpServer.listen(DASHBOARD_PORT, "127.0.0.1", () => {
      process.stderr.write(`[workflow-guardian] Dashboard: http://127.0.0.1:${DASHBOARD_PORT}\n`);
      if (dashboardHeartbeat) {
        clearInterval(dashboardHeartbeat);
        dashboardHeartbeat = null;
      }
    });
  });

  probe.on("timeout", () => probe.destroy());
  probe.end();
}

httpServer.on("error", (err) => {
  if (err.code === "EADDRINUSE") {
    process.stderr.write(`[workflow-guardian] Dashboard port ${DASHBOARD_PORT} taken (race), will retry.\n`);
    if (!dashboardHeartbeat) {
      dashboardHeartbeat = setInterval(tryBindDashboard, HEARTBEAT_INTERVAL_MS);
      dashboardHeartbeat.unref();
    }
  } else {
    process.stderr.write(`[workflow-guardian] Dashboard failed: ${err.message}\n`);
  }
});

function startHeartbeat() {
  if (dashboardHeartbeat) return;
  dashboardHeartbeat = setInterval(tryBindDashboard, HEARTBEAT_INTERVAL_MS);
  dashboardHeartbeat.unref();
}

function register() {
  tryBindDashboard();
  setImmediate(() => {
    if (!httpServer.listening) startHeartbeat();
  });
}

module.exports = { register };
