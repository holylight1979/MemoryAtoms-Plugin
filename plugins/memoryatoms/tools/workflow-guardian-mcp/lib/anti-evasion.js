// anti-evasion.js — anti_evasion_report 工具的 Node 面（扁平模組，不加抽象層）。
//
// one-writer spine：本檔的 tool handler 只回 chip、**不碰 state**；state/持久化
// 由 Python PostToolUse（帶原始 session_id + turn_seq）獨佔（見 hooks/handlers/post_tool_use.py）。
// 報告落在 workflow/aec-report/<sid>-t<turn>.json；要給人看的頁面與 API 不在核心，由插件掛上來。
//
// sendToolResult 來自 mcp.js（循環相依：mcp.handleToolCall lazy-require 本檔，故 tool handler
// 內 lazy-require mcp 即可，載入順序無虞）。

const fs = require("fs");

// ─── severity（純函式；與 Python wg_evasion.aec_severity 同規則、single source of truth）──
// (b) 真偷埋通報非空 → real-evasion；(a) 有真修補行 → notable；(a)(b) 皆「無」/空 → routine。
// (c)–(i) 為資訊性，不升級 severity（severity 只衡量「退避」訊號）。
function aecBlank(v) {
  // 放寬「無」認定含結尾標點（「無。」）與英文的 none／N/A；太嚴會把 routine 誤升 real-evasion → 洗 chat。
  // MIRROR: hooks/wg_evasion.py _aec_blank — keep in sync。
  const s = String(v == null ? "" : v).trim().replace(/[\s。．.,，、；;：:!！?？~～\-—…]+$/u, "");
  return s === "" || /^(?:[無无]|none|n\/?a)\s*(?:[（(][^）)]*[）)])?$/iu.test(s);
}
function aecSeverity(a, b) {
  if (!aecBlank(b)) return "real-evasion";
  if (!aecBlank(a)) return "notable";
  return "routine";
}

// ─── (j) 證據持久路徑／(k) 追蹤項（純函式；MIRROR hooks/wg_verify_ledger.py aec_evidence_missing /
// aec_evidence_autofill / aec_followups_missing；keep in sync）。Node 端不知道本 session 有沒有驗證收據、
// 也不知道 session id（state 與帳本路徑是 Python 的），tool handler 不印 (j)：帶入帳本路徑與缺漏回告由
// Python PostToolUse 做，擋的判定在 Python Stop。
function aecEvidenceMissing(j, receiptsNonEmpty) {
  if (!receiptsNonEmpty) return false;
  if (aecBlank(j)) return true;
  for (const raw of String(j == null ? "" : j).split("\n")) {
    const p = raw.trim().split(" \u2014 ")[0].split(" - ")[0].trim().replace(/^`|`$/g, "");
    if (!p) continue;
    let ok = false;
    try { ok = fs.existsSync(p.replace(/^~(?=[\\/]|$)/, require("os").homedir())); } catch { ok = false; }
    if (!ok) return true;
  }
  return false;
}
// 有收據、j 空／「無」、帳本檔在 → 回帳本絕對路徑；其餘回原 j（模型填了別的路徑就用模型填的）。
function aecEvidenceAutofill(j, receiptsNonEmpty, ledger) {
  let has = false;
  try { has = fs.existsSync(ledger); } catch { has = false; }
  if (receiptsNonEmpty && aecBlank(j) && has) return ledger;
  return String(j == null ? "" : j);
}
function aecFollowupsMissing(k) {
  return String(k == null ? "" : k).trim() === "";
}

// ─── 收尾報告的三類（給看 HUD 的人分類；抓退避也靠它：目標那類一句帶過、AI 自己做主那類一大串，攤開就看得出來）──
// goal＝使用者這次要的事（一件一項，掛使用者原話）；principles＝碰到使用者長期原則／方向時怎麼取捨；
// ai_decided＝使用者沒要求、AI 自己決定的事。每類每項可附 tech（檔:行、commit、規則碼），HUD 收進摺疊。
// 欄位表是 schema（mcp.js）、拒收檢查（下面）、HUD 標籤（aec-hud-html.js）共用的這一份。
const { t } = require("./i18n");
const REPORT_CLASSES = {
  goal: { required: ["said", "done", "verified", "not_done"], min: 1 },
  principles: { required: ["principle", "choice"], min: 0 },
  ai_decided: { required: ["kind", "what", "why"], min: 0 },
};
const AI_DECIDED_KINDS = {
  get tradeoff() { return t("mcp.anti_evasion.kind_tradeoff"); },
  get out_of_scope() { return t("mcp.anti_evasion.kind_out_of_scope"); },
  get assumption() { return t("mcp.anti_evasion.kind_assumption"); },
  get side_effect() { return t("mcp.anti_evasion.kind_side_effect"); },
};
const CLASS_HINT = {
  get goal() { return t("mcp.anti_evasion.hint_goal"); },
  get principles() { return t("mcp.anti_evasion.hint_principles"); },
  get ai_decided() { return t("mcp.anti_evasion.hint_ai_decided"); },
};
function filled(v) { return typeof v === "string" && v.trim() !== ""; }
function classErrors(name, list) {
  const spec = REPORT_CLASSES[name];
  if (!Array.isArray(list)) return [t("mcp.anti_evasion.class_required", { name, hint: CLASS_HINT[name] })];
  if (list.length < spec.min) return [t("mcp.anti_evasion.class_min", { name, min: spec.min, hint: CLASS_HINT[name] })];
  const errs = [];
  list.forEach((it, i) => {
    const item = it && typeof it === "object" ? it : {};
    const missing = spec.required.filter((k) => !filled(item[k]));
    if (missing.length) errs.push(t("mcp.anti_evasion.item_missing", { name, index: i + 1, missing: missing.join(t("mcp.anti_evasion.sep_comma")) }));
    if (name === "ai_decided" && filled(item.kind) && !(item.kind in AI_DECIDED_KINDS)) {
      errs.push(t("mcp.anti_evasion.kind_invalid", { index: i + 1, kinds: Object.keys(AI_DECIDED_KINDS).join(t("mcp.anti_evasion.sep_slash")) }));
    }
  });
  return errs;
}

// ─── MCP tool handler（結構化 emit 表面；不碰 state）────────────────────────────
// 回條一行（報告任何理由都不進聊天）。要模型補的事（(k) 空字串、(d)/(h) 把記憶寫入推到之後）
// 由 Python PostToolUse 用 additionalContext 回告、Stop 擋，判定只留 Python 一份。
const SEV_WORD = {
  get routine() { return t("mcp.anti_evasion.sev_routine"); },
  get notable() { return t("mcp.anti_evasion.sev_notable"); },
  get "real-evasion"() { return t("mcp.anti_evasion.sev_real_evasion"); },
};
function aecChipText(args) {
  const a = args || {};
  const sev = aecSeverity(a.a, a.b);
  const goal = Array.isArray(a.goal) ? a.goal : null;
  const open = goal ? goal.filter((x) => x && !aecBlank(x.not_done)).length : 0;
  const counts = goal
    ? t("mcp.anti_evasion.chip_counts", {
        goal: goal.length, open: open ? t("mcp.anti_evasion.chip_open", { open }) : "",
        principles: (a.principles || []).length, ai: (a.ai_decided || []).length,
      })
    : "";
  return t("mcp.anti_evasion.chip", { severity: SEV_WORD[sev] || sev, counts });
}

// 三類每次都要交（守門開關都一樣）；e／f 的內容歸 ai_decided，不收也不要求。
// g（版控收尾）必填：Stop 的同步提醒看「收尾檢核交代過版控」就不再另擋（SYNC_COVERED_BY_CLOSING_CHECK）。
// harvest_items 跟 knowledge_harvest_report 的 items 同一套驗法（守門開時才收）。
function aecArgErrors(args, v6) {
  const a = args || {};
  const errs = [];
  for (const name of Object.keys(REPORT_CLASSES)) errs.push(...classErrors(name, a[name]));
  if (typeof a.g !== "string") errs.push(t("mcp.anti_evasion.g_required"));
  if (v6 && a.harvest_items !== undefined) {
    if (!Array.isArray(a.harvest_items)) errs.push(t("mcp.anti_evasion.harvest_items_array"));
    else errs.push(...require("./harvest").validateItems(a.harvest_items).map((e) => t("mcp.anti_evasion.harvest_items_error", { error: e })));
  }
  return errs;
}

async function toolAntiEvasionReport(id, args) {
  const { sendToolResult } = require("./mcp");
  const { v6Enabled } = require("./paths");
  const v6 = v6Enabled();
  const errs = aecArgErrors(args, v6);
  if (errs.length) {
    return sendToolResult(id, t("mcp.anti_evasion.rejected") + "\n" + errs.map((e) => "  ✗ " + e).join("\n"), true);
  }
  const hv = v6 && Array.isArray(args.harvest_items) ? "；" + require("./harvest").harvestChip(args.harvest_items) : "";
  return sendToolResult(id, aecChipText(args) + hv);
}

module.exports = {
  aecBlank, aecSeverity, aecEvidenceMissing, aecEvidenceAutofill, aecFollowupsMissing,
  REPORT_CLASSES, AI_DECIDED_KINDS,
  aecChipText, aecArgErrors, toolAntiEvasionReport,
};
