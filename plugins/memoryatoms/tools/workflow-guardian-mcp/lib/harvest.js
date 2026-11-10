// harvest.js — MCP tool `knowledge_harvest_report` 的 Node 面（階段完工知識收割回報）。
//
// one-writer spine：本 handler 只驗 schema、統計、回一行 chip；**不碰 state、不寫檔**。
// items ↔ 本 session atom receipt 的逐項核對、state.knowledge_harvest 分區與 ledger 寫入，
// 全由 Python PostToolUse（帶原始 session_id + turn_seq）獨佔。
//
// sendToolResult 來自 mcp.js（mcp.handleToolCall lazy-require 本檔，handler 內 lazy-require 即可）。

const { t } = require("./i18n");
const HARVEST_REJECTED_TAG = "[Guardian:HarvestRejected] ";   // 拒收訊息的固定開頭，hooks/wg_harvest.is_error_response 靠它辨識（不靠語言文字）
const SOURCES = ["correction", "mechanism", "external_fact", "decision", "atom_fix", "atom_retire"];
const ACTIONS = ["created", "appended", "replaced", "superseded", "retired", "skip"];
const WRITE_ACTIONS = new Set(["created", "appended", "replaced", "superseded"]);
// 萃取路由：part_doc=有部位→該部位病灶文件＋卡一行指標；global=去專案細節後跨專案；org=全公司；
// local=只在記憶系統；shared=專案；candidate_global=realm 拒後先落 shared 標「候選全域」。皆選填。
const { v6Enabled } = require("./paths");
const ROUTES = ["part_doc", "global", "org", "local", "shared", "candidate_global"];

/** 逐 item 驗必填；回錯誤字串陣列（空＝合格）。 */
function validateItems(items) {
  const errs = [];
  items.forEach((it, i) => {
    const tag = `items[${i}]`;
    if (!it || typeof it !== "object") { errs.push(t("mcp.harvest.item_object", { tag })); return; }
    if (!SOURCES.includes(it.source)) errs.push(t("mcp.harvest.source_invalid", { tag, value: it.source, allowed: SOURCES.join("|") }));
    if (!ACTIONS.includes(it.action)) errs.push(t("mcp.harvest.action_invalid", { tag, value: it.action, allowed: ACTIONS.join("|") }));
    if (!String(it.summary || "").trim()) errs.push(t("mcp.harvest.summary_required", { tag }));
    const v6 = v6Enabled();   // 萃取路由欄位（route／part／candidate_global）歸 v6 守門總開關；關閉時不驗
    if (v6 && it.route !== undefined && !ROUTES.includes(it.route)) errs.push(t("mcp.harvest.route_invalid", { tag, value: it.route, allowed: ROUTES.join("|") }));
    if (v6 && it.route === "part_doc" && !String(it.part || "").trim()) errs.push(t("mcp.harvest.part_required", { tag }));
    if (v6 && it.candidate_global !== undefined && typeof it.candidate_global !== "boolean") errs.push(t("mcp.harvest.candidate_bool", { tag }));
    if (v6 && it.route === "candidate_global" && it.candidate_global === false) errs.push(t("mcp.harvest.candidate_conflict", { tag }));
    if (it.action === "skip") {
      if (!String(it.reason || "").trim()) errs.push(t("mcp.harvest.skip_reason", { tag }));
      return;
    }
    if (!String(it.atom || "").trim()) errs.push(t("mcp.harvest.need_atom", { tag, action: it.action }));
    if (!String(it.path || "").trim()) {
      errs.push(t("mcp.harvest.need_path", { tag, action: it.action }));
    }
  });
  return errs;
}

function harvestChip(items) {
  if (!items.length) return t("mcp.harvest.chip_none");
  let written = 0, retired = 0, skipped = 0;
  for (const it of items) {
    if (WRITE_ACTIONS.has(it.action)) written++;
    else if (it.action === "retired") retired++;
    else skipped++;
  }
  const routes = {};
  for (const it of items) if (it.route) routes[it.route] = (routes[it.route] || 0) + 1;
  const routeText = Object.keys(routes).length
    ? "｜route: " + Object.entries(routes).map(([k, v]) => `${k} ${v}`).join(", ")
    : "";
  return t("mcp.harvest.chip_counts", { written, retired, skipped, route: routeText });
}

async function toolKnowledgeHarvestReport(id, args) {
  const { sendToolResult } = require("./mcp");
  const items = args && args.items;
  if (!Array.isArray(items)) {
    return sendToolResult(id, t("mcp.harvest.items_array"), true);
  }
  const errs = validateItems(items);
  if (errs.length) {
    return sendToolResult(id,
      HARVEST_REJECTED_TAG + t("mcp.harvest.rejected") + "\n" + errs.map((e) => "  ✗ " + e).join("\n") +
      "\n" + t("mcp.harvest.rejected_tail"),
      true);
  }
  return sendToolResult(id, harvestChip(items));
}

module.exports = { toolKnowledgeHarvestReport, validateItems, harvestChip, SOURCES, ACTIONS, ROUTES };
