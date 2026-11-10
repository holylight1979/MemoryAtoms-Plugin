// hud-api.js — 收尾報告 HUD 的唯讀 API：glob disk 上 Python 落的 aec-report/<sid>-t<turn>.json 供頁 +
// heartbeat（判窗死用）。港口持有者供頁、與哪個 session 的 MCP 跑了 tool 無關。
//
// 兩個檔、都由 Python 單一寫入（Node 不寫，維持 one-writer spine）：
//   report（<sid>-t<turn>.json）       = Python PostToolUse 寫 / Node 讀（唯讀 API 供 HUD）
//   ledger（aec-tempfiles/<sid>.jsonl）= Python 寫（handlers/aec_ledger.py）/ Node 不直接讀：
//     列表與保留／刪除都轉呼叫 tools/session-artifacts.py（現場查還在不在、當場刪、結果由它寫回帳本）

const fs = require("fs");
const path = require("path");
const { execFile } = require("child_process");
const CORE_LIB = path.join(process.env.WG_CORE_ROOT || path.resolve(__dirname, "..", "..", ".."), "tools", "workflow-guardian-mcp", "lib");   // 核心：插件版由 MCP 載入器給 WG_CORE_ROOT，安裝版是本檔往上三層
const { WORKFLOW_DIR, CODE_DIR, PYTHON_EXE } = require(path.join(CORE_LIB, "paths"));
const { t } = require(path.join(CORE_LIB, "i18n"));   // 本插件的字在 extensions/dashboard/i18n/（鍵 ext.dashboard.*）

// HUD 頁心跳（記憶體，港口持有者持有）；apiAecBeat 更新、apiAecBeatStatus 回 age_s + clients。
let lastHudBeat = 0;

const REPORT_DIR = path.join(WORKFLOW_DIR, "aec-report");  // per-turn 報告檔子夾（檔名 <sid>-t<turn>.json）
const REPORT_CAP = 100;               // 唯讀 API 回傳上限（近 N 筆）
const SID_RE = /^[A-Za-z0-9-]+$/;     // 防路徑穿越：session_id 只允許 hex/hyphen

// ─── HUD 唯讀 API ─────────────────────────────────────────────────────────────
function _json(res, code, data) {
  res.writeHead(code, { "Content-Type": "application/json" });
  res.end(JSON.stringify(data));
}

// glob WORKFLOW_DIR/aec-report/*.json → 依 at(→turn_seq) 新→舊排序。Fail-open 回 []。
function _readReports() {
  let files;
  try { files = fs.readdirSync(REPORT_DIR); }
  catch { return []; }
  const out = [];
  for (const f of files) {
    if (!f.endsWith(".json")) continue;   // 略過 .tmp（atomic write 過渡檔）
    try {
      out.push(JSON.parse(fs.readFileSync(path.join(REPORT_DIR, f), "utf-8")));
    } catch { /* skip corrupt / partial */ }
  }
  out.sort((x, y) =>
    String(y.at || "").localeCompare(String(x.at || "")) ||
    (Number(y.turn_seq) || 0) - (Number(x.turn_seq) || 0)
  );
  return out;
}

// GET /api/aec/reports[?since=<iso>] — 最新卡 + 歷史格清單（增量：at > since）。
function apiAecReports(req, res, since) {
  let reports = _readReports();
  if (since) reports = reports.filter((r) => String(r.at || "") > since);
  _json(res, 200, { reports: reports.slice(0, REPORT_CAP) });
}

// GET /api/aec/report/<sid>/<turn> — 單一回合完整報告（歷史格點開）。
function apiAecReport(req, res, sid, turn) {
  if (!SID_RE.test(String(sid || "")) || !/^\d+$/.test(String(turn || ""))) {
    return _json(res, 404, { error: "bad params" });
  }
  const p = path.join(REPORT_DIR, `${sid}-t${turn}.json`);
  try {
    _json(res, 200, JSON.parse(fs.readFileSync(p, "utf-8")));
  } catch {
    _json(res, 404, { error: "not found" });
  }
}

// GET /api/aec/beat — HUD 頁心跳（主執行緒剛把報告畫上去才打；頁被瀏覽器凍結/節流時會停）。
function apiAecBeat(req, res) {
  lastHudBeat = Date.now();
  _json(res, 200, { ok: true });
}

// GET /api/aec/stream — HUD 頁常駐連線（SSE，內容不走這條、只當存在證明）。
// 窗開著＝連線在：Edge --app 視窗被遮住久了瀏覽器會休眠/凍結頁面、心跳全停，但 TCP 連線
// 仍掛著；視窗關閉／分頁丟棄才斷。hudClients.size 是 Python 判「窗開著」的主證據，心跳為輔。
const hudClients = new Set();
let hudPing = null;
function apiAecStream(req, res) {
  res.writeHead(200, {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
  });
  res.write(": hud\n\n");
  hudClients.add(res);
  const drop = () => { hudClients.delete(res); };
  res.on("close", drop);
  res.on("error", drop);   // 對端消失後的寫入錯誤：剔除，不得 unhandled 'error' 炸掉行程
  if (!hudPing) {
    // 15s 註解 ping：對端死掉（瀏覽器崩潰、機器休眠）時寫入失敗 → close → 剔除，連線數不虛高
    hudPing = setInterval(() => {
      for (const c of hudClients) {
        try { c.write(": ping\n\n"); } catch { hudClients.delete(c); }
      }
      if (!hudClients.size) { clearInterval(hudPing); hudPing = null; }
    }, 15000);
    hudPing.unref();
  }
}

// GET /api/aec/beat-status — Python _hud_alive 判窗用：clients（HUD 頁連線數）為主、age_s 為輔。
function apiAecBeatStatus(req, res) {
  const age_s = lastHudBeat ? Math.round((Date.now() - lastHudBeat) / 1000) : 999999;
  _json(res, 200, { age_s, clients: hudClients.size });
}

// ─── 產出物帳本（Python 寫 aec-tempfiles/<sid>.jsonl；查與刪都轉呼叫 tools/session-artifacts.py）────
// 「還在不在、能不能刪」要問 git（工作樹、分支）與保護規則，只在 Python 留一份；Node 只轉呼叫。
// 列表快取 10 秒（HUD 每 1.5 秒輪詢，不能每次都起 Python）；帳本變了或按了按鈕就作廢。
const LEDGER_DIR = path.join(WORKFLOW_DIR, "aec-tempfiles");
const ARTIFACTS_CLI = path.join(CODE_DIR, "tools", "session-artifacts.py");
const LIST_TTL_MS = 10000;
const _listCache = new Map();   // sid → {at, mtime, data, pending}

function _runArtifacts(args, timeoutMs) {
  // 插件版不出廠 session-artifacts.py（它和它讀的帳本 handlers/aec_ledger.py 都是收尾檢核，守門家族）：不起 Python，回一句「完整版才有」
  if (!fs.existsSync(ARTIFACTS_CLI)) {
    return Promise.resolve({ ok: false, error: true, unavailable: true, message: t("ext.dashboard.artifacts_full_install_only") });
  }
  return new Promise((resolve) => {
    execFile(PYTHON_EXE, ["-X", "utf8", ARTIFACTS_CLI, ...args, "--json"],
      { cwd: CODE_DIR, windowsHide: true, timeout: timeoutMs, encoding: "utf-8", maxBuffer: 4 * 1024 * 1024 },
      (err, stdout, stderr) => {
        const first = String(stdout || "").split("\n")[0].trim();
        try { return resolve(JSON.parse(first)); } catch { /* fall through */ }
        resolve({ ok: false, error: true,
          message: t("ext.dashboard.list_tool_silent_detail", { detail: String((err && err.message) || stderr || t("ext.dashboard.empty_output")).slice(0, 200) }) });
      });
  });
}

function _ledgerMtime(sid) {
  try { return fs.statSync(path.join(LEDGER_DIR, `${sid}.jsonl`)).mtimeMs; } catch { return 0; }
}

// GET /api/aec/tempfiles/<sid> — 這個 session 帳上還在的東西（含白話狀態、上次保留／刪除的結果）。
function apiAecTempfiles(req, res, sid) {
  if (!SID_RE.test(String(sid || ""))) return _json(res, 404, { error: "bad params" });
  const mtime = _ledgerMtime(sid);
  if (!mtime) return _json(res, 200, { sid, items: [] });
  const c = _listCache.get(sid);
  if (c && c.data && c.mtime === mtime && Date.now() - c.at < LIST_TTL_MS) return _json(res, 200, c.data);
  const entry = c && c.pending ? c : { pending: null, data: c && c.data, at: 0, mtime: 0 };
  if (!entry.pending) {
    entry.pending = _runArtifacts(["list", "--sid", sid], 30000).then((data) => {
      const ok = data && Array.isArray(data.items);
      Object.assign(entry, ok ? { data, at: Date.now(), mtime } : {}, { pending: null });
      return ok ? data : (entry.data || { sid, items: [], error: data && data.message });
    });
    _listCache.set(sid, entry);
  }
  entry.pending.then((data) => _json(res, 200, data));
}

// GET /api/aec/tempfiles-all — 全機殘留：全部帳本裡還在的東西，依 session 分組（附 session 還開著沒有）。
// 掃全部帳本要查 git，比單一 session 慢：快取 30 秒、同時只跑一支；按了任何保留／刪除就作廢。
const ALL_TTL_MS = 30000;
const _allCache = { at: 0, data: null, pending: null };
function apiAecTempfilesAll(req, res) {
  if (_allCache.data && Date.now() - _allCache.at < ALL_TTL_MS) return _json(res, 200, _allCache.data);
  if (!_allCache.pending) {
    _allCache.pending = _runArtifacts(["list", "--all"], 120000).then((data) => {
      const ok = data && Array.isArray(data.sessions);
      if (ok) Object.assign(_allCache, { data, at: Date.now() });
      _allCache.pending = null;
      return ok ? data : { sessions: [], total: 0, error: (data && data.message) || t("ext.dashboard.list_tool_silent") };
    });
  }
  _allCache.pending.then((data) => _json(res, 200, data));
}

function _readBody(req, cb) {
  let body = "";
  req.on("data", (ch) => (body += ch));
  req.on("end", () => { try { cb(body ? JSON.parse(body) : {}); } catch { cb(null); } });
}

// POST /api/aec/decision — HUD 的保留／刪除鈕：body {sid, key, action:"keep"|"delete", force?}；
// 全機殘留的「整組清」：body {sid, all_safe:true, action:"delete"}（只刪不用再確認的，Python 現場判）。
// 當場交給 Python 執行並回 {ok, message, needs_confirm}；needs_confirm 有值＝要使用者再確認一次（HUD 帶 force 重送）。
// key 只能指名帳上那一項（Python 端比對帳本），不能拿來刪任意路徑。loopback 信任模型：無 auth，僅 127.0.0.1。
function apiAecDecisionPost(req, res) {
  _readBody(req, (body) => {
    if (!body) return _json(res, 400, { error: "bad json" });
    const sid = String(body.sid == null ? "" : body.sid);
    const key = String(body.key == null ? "" : body.key);
    const action = String(body.action == null ? "" : body.action);
    const allSafe = body.all_safe === true;
    if (!SID_RE.test(sid) || (!allSafe && (!key || key.length > 2000 || /[\r\n]/.test(key)))) {
      return _json(res, 404, { error: "bad params" });
    }
    if (action !== "keep" && action !== "delete") return _json(res, 400, { error: "bad action" });
    if (allSafe && (action !== "delete" || body.force)) return _json(res, 400, { error: t("ext.dashboard.all_safe_delete_only") });
    const args = allSafe ? ["act", "--sid", sid, "--all-safe", "--action", "delete"]
      : ["act", "--sid", sid, "--key", key, "--action", action, ...(body.force ? ["--force"] : [])];
    _runArtifacts(args, allSafe ? 300000 : 90000).then((r) => {
      _listCache.delete(sid);
      Object.assign(_allCache, { data: null, at: 0 });
      _json(res, 200, r);
    });
  });
}

module.exports = {
  apiAecReports, apiAecReport, apiAecBeat, apiAecBeatStatus, apiAecStream,
  apiAecTempfiles, apiAecTempfilesAll,
  apiAecDecisionPost,
};
