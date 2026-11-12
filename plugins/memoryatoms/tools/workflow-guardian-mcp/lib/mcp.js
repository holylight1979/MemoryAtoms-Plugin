// mcp.js — MCP stdio JSON-RPC transport。buffer 私有於本檔（stdin loop + handleMessage 同檔）。
// 規則 MCP_INSTRUCTIONS_NON_CC_ONLY：initialize 只對非 Claude Code 客戶端帶 instructions；memory_search 標 alwaysLoad（hooks/wg_release_rules.py）
// handleToolCall lazy-require atom-tools 以化解 mcp<->atom-tools 循環相依。
const { VERSIONS } = require("./paths");
const { crashLog } = require("./log");
const { t } = require("./i18n");
const CONF = { fixed: "固", observed: "觀", tentative: "臨" };   // 信心標記字面值，語言表用 {fixed} 等代入（標記本身不翻）
// anti-evasion 是守門家族：插件包不帶這支檔，缺檔就不提供 anti_evasion_report（不加設定開關）。頂層不 require 本檔，無循環。
let AI_DECIDED_KINDS = {};
let HAS_AEC = false;
try {
  ({ AI_DECIDED_KINDS } = require("./anti-evasion"));
  HAS_AEC = true;
} catch (e) {
  if (e.code !== "MODULE_NOT_FOUND") throw e;
}

// ─── MCP Protocol ───────────────────────────────────────────────────────────

let buffer = "";

process.stdin.setEncoding("utf-8");
process.stdin.on("data", (chunk) => {
  buffer += chunk;
  processBuffer();
});

function processBuffer() {
  // Newline-delimited JSON (Claude Code 2.x transport format)
  let line;
  while ((line = extractLine()) !== null) {
    if (!line.trim()) continue;
    try {
      const parsed = JSON.parse(line);
      handleMessage(parsed);
    } catch (err) {
      crashLog("PARSE_ERROR", err);
      sendError(null, -32700, "Parse error");
    }
  }
}

function extractLine() {
  // Try newline-delimited first (what Claude Code actually sends)
  const nlIdx = buffer.indexOf("\n");
  if (nlIdx !== -1) {
    const line = buffer.slice(0, nlIdx);
    buffer = buffer.slice(nlIdx + 1);
    return line;
  }
  return null;
}

function sendResponse(id, result) {
  const msg = JSON.stringify({ jsonrpc: "2.0", id, result });
  process.stdout.write(msg + "\n");
}

function sendError(id, code, message) {
  const msg = JSON.stringify({ jsonrpc: "2.0", id, error: { code, message } });
  process.stdout.write(msg + "\n");
}

// ─── Server instructions（給沒有 hook 的客戶端）────────────────────────────────
// 開場就會被客戶端讀到的指路（工具被延遲載入時也照樣送到）。Claude Code 有 hook 每回合注入與 rules/core.md，不重送。
const CLAUDE_CODE_CLIENT = "claude-code";   // Claude Code 在 initialize 帶的 clientInfo.name（claude.exe 內 MCP client 的 name）
const CLIENT_INSTRUCTIONS = [
  t("mcp.instructions.intro"),
  t("mcp.instructions.search"),
  t("mcp.instructions.write"),
  t("mcp.instructions.where"),
].join("\n");

function initializeResult(params) {
  // clientInfo.name 是 Claude Code 才省略 instructions；判不出（沒帶 clientInfo）就送。
  const result = {
    protocolVersion: "2025-11-25",
    capabilities: { tools: { listChanged: false } },
    serverInfo: { name: "workflow-guardian", version: VERSIONS.guardian },
  };
  if (params?.clientInfo?.name !== CLAUDE_CODE_CLIENT) result.instructions = CLIENT_INSTRUCTIONS;
  return result;
}

// ─── MCP Message Handler ────────────────────────────────────────────────────

function handleMessage(msg) {
  const { id, method, params } = msg;

  switch (method) {
    case "initialize":
      sendResponse(id, initializeResult(params));
      break;

    case "notifications/initialized":
      break;

    case "tools/list":
      sendResponse(id, { tools: TOOL_DEFINITIONS });
      break;

    case "tools/call":
      handleToolCall(id, params?.name, params?.arguments || {});
      break;

    default:
      if (id !== undefined) {
        sendError(id, -32601, `Method not found: ${method}`);
      }
  }
}

// ─── Tool Definitions ───────────────────────────────────────────────────────

const TOOL_DEFINITIONS = [
  {
    name: "atom_write",
    description:
      "Write or update an atom file with validated format. " +
      "Ensures correct metadata structure, runs write-gate dedup, " +
      "updates MEMORY.md index, and triggers vector indexing. " +
      "V4: supports shared/role/personal scopes; sensitive audience " +
      "(architecture/decision) on shared auto-routes to _pending_review/.",
    inputSchema: {
      type: "object",
      properties: {
        title: { type: "string", description: "Atom title (becomes # heading and filename slug)" },
        scope: {
          type: "string",
          enum: ["global", "shared", "role", "personal", "project", "org"],
          description: t("mcp.atom_write.param_scope"),
        },
        role: {
          type: "string",
          description: "Role subdir name (e.g. art, programmer, planner). Required when scope=role.",
        },
        user: {
          type: "string",
          description: "Personal subdir owner. Required when scope=personal; falls back to current OS user.",
        },
        cross_project: {
          type: "boolean",
          description: "scope=personal only. true = the user's CROSS-PROJECT personal preference, stored at ~/.claude/memory/personal/<user>/ and visible to that user in every project (default false = personal-in-project at {proj}/.claude/memory/personal/<user>/, visible only in that project). Calls made from ~/.claude itself land cross-project automatically.",
        },
        secret: {
          type: "boolean",
          description: t("mcp.atom_write.param_secret"),
        },
        secret_reason: {
          type: "string",
          description: t("mcp.atom_write.param_secret_reason"),
        },
        not_sensitive: {
          type: "string",
          description: t("mcp.atom_write.param_not_sensitive"),
        },
        audience: {
          type: "array", items: { type: "string" },
          description: "Audience tags (multi-role). On scope=shared, presence of 'architecture' or 'decision' auto-routes atom to _pending_review/ with Pending-review-by: management.",
        },
        pending_review_by: {
          type: "string",
          description: "Optional Pending-review-by metadata (e.g. 'management'). Auto-set for sensitive audience on shared.",
        },
        merge_strategy: {
          type: "string", enum: ["ai-assist", "git-only"],
          description: "Optional Merge-strategy metadata. Default ai-assist (omitted from file).",
        },
        realm: {
          type: "string", enum: ["core", "local"],
          description: "V5+ realm (orthogonal to scope; only effective when scope=global). 'core' (default) = cross-project knowledge in memory/, injected in every project. 'local' = ~/.claude-only knowledge routed to memory/_local/<domain>/, injected ONLY when the working dir is under ~/.claude (zero cost in external projects). Use 'local' for memory-system/tooling/brain-world knowledge that is irrelevant outside ~/.claude. The atom keeps scope=global; realm is derived from its path, not stored as a field.",
        },
        domain: {
          type: "string",
          description: t("mcp.atom_write.param_domain"),
        },
        dry_run: {
          type: "boolean",
          description: t("mcp.atom_write.param_dry_run", CONF),
        },
        allow_new_category: {
          type: "boolean",
          description: "Allow `domain` to open a NEW Lv1 category not in taxonomy.json (still subject to reserved-name / charset checks). Default false. Prefer an existing Lv1; new Lv1s should be rare and deliberate.",
        },
        subdir: {
          type: "string",
          description: "Optional create-target subdir relative to the project memory root (slash-separated, scope=shared only), e.g. 'projects/AI-gen-X' → memory/projects/AI-gen-X/<slug>.md. Supports one-repo-multi-project partition layouts in a single write. Segments are sandboxed (no '..', no '_' prefix, protected dirs like personal/roles rejected). Only affects the create landing spot — append/replace locate the existing file via the index regardless of subfolder.",
        },
        confidence: { type: "string", enum: ["[固]", "[觀]", "[臨]"], description: "Confidence level" },
        triggers: {
          type: "array", items: { type: "string" },
          description: "Trigger keywords for MEMORY.md index",
        },
        knowledge: {
          type: "array", items: { type: "string" },
          description: t("mcp.atom_write.param_knowledge", CONF),
        },
        actions: {
          type: "array", items: { type: "string" },
          description: "Action guidelines",
        },
        related: {
          type: "array", items: { type: "string" },
          description: "Related atom names (optional)",
        },
        supersedes: {
          type: "array", items: { type: "string" },
          description: t("mcp.atom_write.param_supersedes"),
        },
        provenance: {
          type: "string",
          description: t("mcp.atom_write.param_provenance"),
        },
        depends: {
          type: "array", items: { type: "string" },
          description: "Optional validity dependencies, e.g. [\"path:~/.claude/workflow/config.json\"] (root-layer card; ~/.claude/ stands for the data root) or [\"path:src/entrypoint.py\"] (project card, relative to project root). Cards are shared across machines: never write this machine's absolute path (C:/Users/<me>/...). Rendered as `- Depends:`; path: entries are machine-checked by health-check (missing → stale). replace: omit=keep, []=clear.",
        },
        entry: {
          type: "array", items: { type: "string" },
          description: t("mcp.atom_write.param_entry"),
        },
        quote: {
          type: "string",
          description: t("mcp.atom_write.param_quote"),
        },
        status: {
          type: "string",
          description: t("mcp.atom_write.param_status"),
        },
        kind_hint: {
          type: "string",
          enum: ["pointer", "complement", "ledger", "hub", "plain"],
          description: t("mcp.atom_write.param_kind_hint"),
        },
        dedupe: {
          type: "object",
          properties: {
            verdict: { type: "string", enum: ["same", "supersedes", "different"] },
            of: { type: "string" },
            why: { type: "string" },
          },
          description: t("mcp.atom_write.param_dedupe"),
        },
        conflict: {
          type: "array",
          items: {
            type: "object",
            properties: {
              of: { type: "string" },
              verdict: { type: "string", enum: ["conflict", "not_conflict"] },
              why: { type: "string" },
            },
          },
          description: t("mcp.atom_write.param_conflict"),
        },
        scope_reason: {
          type: "string",
          description: t("mcp.atom_write.param_scope_reason"),
        },
        mode: {
          type: "string", enum: ["create", "append", "replace"],
          description: "create=new atom, append=add knowledge lines, replace=overwrite knowledge section",
        },
        project_cwd: {
          type: "string",
          description: "Project root path (required for scope=shared/role/personal). For scope=global it feeds the realm gate (project-name scan); omitted → the MCP process cwd (= session cwd) is used.",
        },
        skip_gate: {
          type: "boolean",
          description: t("mcp.atom_write.param_skip_gate", CONF),
        },
        skip_conflict_check: {
          type: "boolean",
          description: "Skip write-time conflict detection (shared scope only). Use only for controlled migrations / tests.",
        },
      },
      required: ["title", "confidence", "triggers", "knowledge", "mode"],
    },
  },
  {
    name: "atom_promote",
    description:
      t("mcp.atom_promote.description", CONF),
    inputSchema: {
      type: "object",
      properties: {
        atom_name: { type: "string", description: "Atom filename without .md extension" },
        scope: { type: "string", enum: ["global", "project"], description: "Scope to search in" },
        project_cwd: { type: "string", description: "Project root (required for project scope)" },
        execute: { type: "boolean", description: "true=execute promotion, false=dry-run check only" },
        merge_to_preferences: {
          type: "boolean",
          description: t("mcp.atom_promote.param_merge_to_preferences", CONF),
        },
      },
      required: ["atom_name", "scope", "execute"],
    },
  },
  {
    name: "atom_feedback",
    description:
      t("mcp.atom_feedback.description", CONF),
    inputSchema: {
      type: "object",
      properties: {
        atom_name: { type: "string", description: "Atom filename without .md extension" },
        verdict: { type: "string", enum: ["helpful", "misleading"], description: "helpful = the card saved you from a mistake; misleading = it sent you the wrong way" },
        scope: { type: "string", enum: ["global", "project"], description: "Scope to search in (default global)" },
        project_cwd: { type: "string", description: "Project root (required for project scope)" },
        note: { type: "string", description: "Why you voted this way (max 200 chars)" },
      },
      required: ["atom_name", "verdict"],
    },
  },
  {
    name: "atom_move",
    description:
      "V5 SoT-correct atom move/reconcile. Updates the single central _atom_index.json (via upsert/delete) and moves the .access.json sidecar with the .md; the _ATOM_INDEX.md mirror is auto-regenerated — it does NOT hand-edit per-folder indexes. " +
      "subcommand='move' relocates an atom to --to (a memory-root OR a subfolder under one — index root is auto-detected; the index scope is always preserved unless explicitly overridden via `scope`). " +
      "subcommand='reconcile' assumes the atom was manually moved to --at and fixes its index path + cross-layer refs. " +
      "Refuses atoms under memory/_local/ (use atom-set-realm) or _AIDocs/Failures/ (title-routed). Cross-root layering: down-refs (global→project) removed, up-refs kept, sibling refs warned. Self-validates via validate_index. Use dry_run=true to preview.",
    inputSchema: {
      type: "object",
      properties: {
        subcommand: { type: "string", enum: ["move", "reconcile"], description: "move=mv + sync JSON SoT + sidecar; reconcile=sync only (atom already at target)" },
        atom: { type: "string", description: "Atom slug (filename without .md)" },
        from: { type: "string", description: "Source dir — atom located via index/slug; index root auto-detected by walking up (required for move)" },
        to: { type: "string", description: "Target folder — a memory-root or any subfolder under one (required for move)" },
        at: { type: "string", description: "Dir at/under the memory-root where the atom now lives (required for reconcile)" },
        scope: { type: "string", description: "Explicitly set the index scope on move (optional). Default: preserve the existing index scope — moves never reset scope on their own; scope_changed in the report is honest." },
        dry_run: { type: "boolean", description: "Preview without applying changes" },
      },
      required: ["subcommand", "atom"],
    },
  },
  {
    name: "atom_edit_meta",
    description:
      "Surgically edit an atom's metadata (Trigger / Related / Tags / Entry) in place — " +
      "no full-file rebuild. Locates <atom_name>.md via py lib/atom_io.locate_atom (single " +
      "authority: global memory/ + memory/Failures/ + memory/_local/; project shared → " +
      "personal(current user) → role when scope=project), then " +
      "delegates to lib/atom_io.edit_metadata through the audit funnel. " +
      "Pass any subset of triggers/related/tags/entry; at least one is required. " +
      "Each provided field fully replaces that field's existing value.",
    inputSchema: {
      type: "object",
      properties: {
        atom_name: { type: "string", description: "Atom filename without .md extension" },
        scope: { type: "string", enum: ["global", "project"], description: "Scope to search in" },
        role: { type: "string", description: "Optional: also try the project's roles/<role>/ layer when scope=project" },
        user: { type: "string", description: "Optional: personal layer owner when scope=project (default current OS user)" },
        triggers: {
          type: "array", items: { type: "string" },
          description: "Replacement Trigger keywords (optional). Replaces the whole Trigger line.",
        },
        related: {
          type: "array", items: { type: "string" },
          description: "Replacement Related atom names (optional). Replaces the whole Related line.",
        },
        tags: {
          type: "array", items: { type: "string" },
          description: "Replacement Tags (optional). Replaces the whole Tags line.",
        },
        entry: {
          type: "array", items: { type: "string" },
          description: t("mcp.atom_edit_meta.param_entry"),
        },
        project_cwd: { type: "string", description: "Project root (required for project scope)" },
      },
      required: ["atom_name", "scope"],
    },
  },
  {
    name: "anti_evasion_report",
    description:
      t("mcp.anti_evasion_report.description"),
    inputSchema: {
      type: "object",
      properties: {
        goal: {
          type: "array",
          description: t("mcp.anti_evasion_report.param_goal"),
          items: {
            type: "object",
            properties: {
              said: { type: "string", description: t("mcp.anti_evasion_report.param_goal_said") },
              done: { type: "string", description: t("mcp.anti_evasion_report.param_goal_done") },
              verified: { type: "string", description: t("mcp.anti_evasion_report.param_goal_verified") },
              not_done: { type: "string", description: t("mcp.anti_evasion_report.param_goal_not_done") },
              tech: { type: "string", description: t("mcp.anti_evasion_report.param_goal_tech") },
            },
            required: ["said", "done", "verified", "not_done"],
          },
        },
        principles: {
          type: "array",
          description: t("mcp.anti_evasion_report.param_principles"),
          items: {
            type: "object",
            properties: {
              principle: { type: "string", description: t("mcp.anti_evasion_report.param_principles_principle") },
              choice: { type: "string", description: t("mcp.anti_evasion_report.param_principles_choice") },
              tech: { type: "string", description: t("mcp.anti_evasion_report.param_principles_tech") },
            },
            required: ["principle", "choice"],
          },
        },
        ai_decided: {
          type: "array",
          description: t("mcp.anti_evasion_report.param_ai_decided"),
          items: {
            type: "object",
            properties: {
              kind: {
                type: "string", enum: Object.keys(AI_DECIDED_KINDS),
                description: t("mcp.anti_evasion_report.param_ai_decided_kind"),
              },
              what: { type: "string", description: t("mcp.anti_evasion_report.param_ai_decided_what") },
              why: { type: "string", description: t("mcp.anti_evasion_report.param_ai_decided_why") },
              tech: { type: "string", description: t("mcp.anti_evasion_report.param_ai_decided_tech") },
            },
            required: ["kind", "what", "why"],
          },
        },
        report: { type: "string", description: t("mcp.anti_evasion_report.param_report") },
        harvest_items: { type: "array", description: t("mcp.anti_evasion_report.param_harvest_items"), items: { type: "object" } },
        a: { type: "string", description: t("mcp.anti_evasion_report.param_a") },
        b: { type: "string", description: t("mcp.anti_evasion_report.param_b") },
        d: { type: "string", description: t("mcp.anti_evasion_report.param_d") },
        g: { type: "string", description: t("mcp.anti_evasion_report.param_g") },
        h: { type: "string", description: t("mcp.anti_evasion_report.param_h") },
        i: { type: "string", description: t("mcp.anti_evasion_report.param_i") },
        j_evidence_path: { type: "string", description: t("mcp.anti_evasion_report.param_j_evidence_path") },
        k_followups: { type: "string", description: t("mcp.anti_evasion_report.param_k_followups") },
      },
      required: ["goal", "principles", "ai_decided", "a", "b", "d", "g", "h", "i", "j_evidence_path", "k_followups"],
    },
  },
  {
    name: "knowledge_harvest_report",
    description:
      t("mcp.knowledge_harvest_report.description"),
    inputSchema: {
      type: "object",
      properties: {
        items: {
          type: "array",
          description: t("mcp.knowledge_harvest_report.param_items"),
          items: {
            type: "object",
            properties: {
              source: {
                type: "string",
                enum: ["correction", "mechanism", "external_fact", "decision", "atom_fix", "atom_retire"],
                description: t("mcp.knowledge_harvest_report.param_source"),
              },
              summary: { type: "string", description: t("mcp.knowledge_harvest_report.param_summary") },
              action: {
                type: "string",
                enum: ["created", "appended", "replaced", "superseded", "retired", "skip"],
                description: t("mcp.knowledge_harvest_report.param_action"),
              },
              atom: { type: "string", description: t("mcp.knowledge_harvest_report.param_atom") },
              path: { type: "string", description: t("mcp.knowledge_harvest_report.param_path") },
              scope: { type: "string", enum: ["global", "shared", "personal", "role", "local", "org"], description: t("mcp.knowledge_harvest_report.param_scope") },
              reason: { type: "string", description: t("mcp.knowledge_harvest_report.param_reason") },
              route: {
                type: "string",
                enum: ["part_doc", "global", "org", "local", "shared", "candidate_global"],
                description: t("mcp.knowledge_harvest_report.param_route"),
              },
              candidate_global: { type: "boolean", description: t("mcp.knowledge_harvest_report.param_candidate_global") },
              part: { type: "string", description: t("mcp.knowledge_harvest_report.param_part") },
            },
            required: ["source", "summary", "action"],
          },
        },
        note: { type: "string", description: t("mcp.knowledge_harvest_report.param_note") },
      },
      required: ["items"],
    },
  },
  {
    name: "atom_retire",
    description:
      t("mcp.atom_retire.description", CONF),
    inputSchema: {
      type: "object",
      properties: {
        atom_name: { type: "string", description: t("mcp.atom_retire.param_atom_name") },
        scope: {
          type: "string", enum: ["global", "shared", "personal", "role", "local", "org"],
          description: t("mcp.atom_retire.param_scope"),
        },
        project_cwd: { type: "string", description: t("mcp.atom_retire.param_project_cwd") },
        role: { type: "string", description: t("mcp.atom_retire.param_role") },
        user: { type: "string", description: t("mcp.atom_retire.param_user") },
        reason: { type: "string", description: t("mcp.atom_retire.param_reason") },
        dry_run: { type: "boolean", description: t("mcp.atom_retire.param_dry_run") },
      },
      required: ["atom_name", "scope", "reason"],
    },
  },
  {
    name: "memory_search",
    description:
      t("mcp.memory_search.description"),
    // Claude Code 工具搜尋：只有這支開場就載入完整 schema（查記憶最常用、schema 小）；其餘 8 支照常延遲。
    _meta: { "anthropic/alwaysLoad": true },
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string", description: t("mcp.memory_search.param_query") },
        cwd: { type: "string", description: t("mcp.memory_search.param_cwd") },
        top_k: { type: "integer", description: t("mcp.memory_search.param_top_k") },
        format: { type: "string", enum: ["table", "json"], description: t("mcp.memory_search.param_format") },
      },
      required: ["query"],
    },
  },
  {
    name: "atom_source",
    description:
      t("mcp.atom_source.description"),
    inputSchema: {
      type: "object",
      properties: {
        atom: { type: "string", description: t("mcp.atom_source.param_atom") },
        cwd: { type: "string", description: t("mcp.atom_source.param_cwd") },
        format: { type: "string", enum: ["table", "json"], description: t("mcp.atom_source.param_format") },
      },
      required: ["atom"],
    },
  },
];
if (!HAS_AEC) TOOL_DEFINITIONS.splice(TOOL_DEFINITIONS.findIndex((d) => d.name === "anti_evasion_report"), 1);

// ─── Tool Handlers ──────────────────────────────────────────────────────────

function handleToolCall(id, toolName, args) {
  const { toolAtomWrite, toolAtomPromote, toolAtomFeedback, toolAtomMove, toolAtomEditMeta, toolAtomRetire, toolMemorySearch, toolAtomSource } = require("./atom-tools");
  switch (toolName) {
    case "memory_search":
      return toolMemorySearch(id, args).catch(e => sendToolResult(id, `memory_search error: ${e.message}`, true));
    case "atom_source":
      return toolAtomSource(id, args).catch(e => sendToolResult(id, `atom_source error: ${e.message}`, true));
    case "atom_retire":
      return toolAtomRetire(id, args).catch(e => sendToolResult(id, `atom_retire error: ${e.message}`, true));
    case "knowledge_harvest_report":
      // one-writer：只驗 schema、回 chip；receipt 核對／state／ledger 由 Python PostToolUse 獨佔。
      return require("./harvest").toolKnowledgeHarvestReport(id, args)
        .catch(e => sendToolResult(id, `knowledge_harvest_report error: ${e.message}`, true));
    case "atom_write":
      return toolAtomWrite(id, args).catch(e => sendToolResult(id, `atom_write error: ${e.message}`, true));
    case "atom_promote":
      // toolAtomPromote 為 async，需 .catch 包 throw（與 atom_write/atom_move 一致）
      return toolAtomPromote(id, args).catch(e => sendToolResult(id, `atom_promote error: ${e.message}`, true));
    case "atom_feedback":
      return toolAtomFeedback(id, args).catch(e => sendToolResult(id, `atom_feedback error: ${e.message}`, true));
    case "atom_move":
      return toolAtomMove(id, args).catch(e => sendToolResult(id, `atom_move error: ${e.message}`, true));
    case "atom_edit_meta":
      return toolAtomEditMeta(id, args).catch(e => sendToolResult(id, `atom_edit_meta error: ${e.message}`, true));
    case "anti_evasion_report":
      if (!HAS_AEC) return sendError(id, -32601, `Unknown tool: ${toolName}`);
      // one-writer：只回 chip、不碰 state（state/持久化/HUD 由 Python PostToolUse 獨佔）。
      return require("./anti-evasion").toolAntiEvasionReport(id, args)
        .catch(e => sendToolResult(id, `anti_evasion_report error: ${e.message}`, true));
    default:
      return callPluginTool(id, toolName, args);
  }
}

// ─── 插件登記的工具（lib/plugins.js 開機時呼叫 registerTool）──────────────────
const pluginTools = new Map();   // 工具名 → handler(args)：回字串或 {text, isError}，可以是 Promise

function registerTool(definition, handler) {
  if (!definition || typeof definition.name !== "string" || typeof handler !== "function") {
    throw new Error("registerTool needs a definition with a name and a handler function");
  }
  if (TOOL_DEFINITIONS.some((t) => t.name === definition.name)) {
    throw new Error(`tool name ${definition.name} is already taken`);
  }
  TOOL_DEFINITIONS.push(definition);
  pluginTools.set(definition.name, handler);
}

function callPluginTool(id, toolName, args) {
  const handler = pluginTools.get(toolName);
  if (!handler) return sendError(id, -32601, `Unknown tool: ${toolName}`);
  return Promise.resolve()
    .then(() => handler(args))
    .then((out) => (typeof out === "string"
      ? sendToolResult(id, out)
      : sendToolResult(id, String(out && out.text || ""), Boolean(out && out.isError))))
    .catch((e) => sendToolResult(id, `${toolName} error: ${e && e.message || e}`, true));
}

function sendToolResult(id, text, isError = false) {
  sendResponse(id, {
    content: [{ type: "text", text }],
    ...(isError && { isError: true }),
  });
}

module.exports = {
  sendResponse, sendError, sendToolResult, handleMessage, handleToolCall, registerTool, TOOL_DEFINITIONS,
};
