---
name: memory
description: "All-in-one memory system tool — health/review/score as the three main commands, plus classify (organize project memory into scope layers; use when SessionStart shows [Guardian:ScopeLayout] or the user says 整理記憶分類) and peek/undo (only for legacy auto-extraction leftovers). Use to check memory health, run self-iteration, and score sessions. 觸發詞：記憶健康, 記憶檢查, 整理記憶分類, 記憶評分, session 評分, memory health, memory review, memory score, classify"
---

# /memoryatoms:memory — Memory system toolkit

> One skill covering six subcommands: health / peek / undo / review / score / classify
> (peek/undo only inspect legacy auto-extraction leftovers). Global skill, works in any project.

---

## Usage

```
/memoryatoms:memory health [--json]
/memoryatoms:memory peek [--since=24h]        # legacy leftovers only (the auto-extraction pipeline has been removed)
/memoryatoms:memory undo [last | --since=24h | --all-from-today]   # same as above
/memoryatoms:memory review
/memoryatoms:memory score [--last | --since=24h | --top-n=10]
/memoryatoms:memory classify                  # organize project memory by scope (personal = you only / project rules go to shared)
```

The first token is the subcommand; parse it from `$ARGUMENTS`.
**No subcommand -> run `health` directly** (no menu, no questions); other subcommands must be given explicitly.

---

### classify -> organize project memory into scope layers

Trigger: SessionStart shows `[Guardian:ScopeLayout]`, or the user says 「整理記憶分類」 (organize memory classification). Run at the project root.

1. **plan (script):**
   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/tools/classify-project-scope.py" plan
   ```
   Outputs JSON: `personal[]` (each personal item: slug / owner / first sentence / `suggest` shared|personal|review / reason / `hits` = matched cue words; review = it only matched cue words like 「此專案」 / 「必須」 / 「上傳」 (this project / must / upload), so it is probably project-specific and the user decides where it goes), `index` (counts of wrong scope tags, dangling entries, trigger drift), `shared_flat`, `classified`.
2. **Ask the user (the only step that needs a human judgment):** turn `personal[]` into a table (number | first-sentence summary | suggested destination) and use AskUserQuestion: "Follow the suggestions (Recommended) / list the numbers of exceptions". Four destinations: `shared` (project rule; Author records the proposer) / `personal` (keep for this user x this project) / `cross_project` (this user across projects; move to `~/.claude/memory/personal/<user>/`) / `reject` (one-off task; move to `_rejected/`). First explain the change in two or three plain sentences: personal is visible only to its owner, project rules must go into shared for others to see them, and memory from other projects is no longer injected.
3. **apply (script):** write the decisions as a `{slug: destination}` JSON file (in the scratchpad),
   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/tools/classify-project-scope.py" apply --decisions <file> --dry-run   # preview first
   python "${CLAUDE_PLUGIN_ROOT}/tools/classify-project-scope.py" apply --decisions <file>             # actually move + write back index + mark
   ```
   If there are no personal items, run `apply` with empty decisions `{}` or just `mark` to set the marker.
4. **Upload:** remind the user to put the changes under `.claude/memory/` into the project's version control (git/svn; personal/ follows the project's .gitignore rules).
5. Done check: `status` returns `classified=true` (`_atom_index.json.layout=="scope-v2"` or `shared/_taxonomy.json` exists); after that SessionStart no longer prompts.

---

## Subcommand dispatch

### health -> memory quality diagnosis

Run `tools/memory-audit.py` and `tools/atom-health-check.py` together:

1. **Detect the project memory directory** (Bash):
   ```bash
   python -c "
   import sys, os
   sys.path.insert(0, r'${CLAUDE_PLUGIN_ROOT}/hooks')
   try:
       from wg_core import get_project_memory_dir
       d = get_project_memory_dir(os.getcwd())
       print(d or '')
   except Exception:
       print('')
   "
   ```
   If non-empty, record it as `$PROJECT_MEM_DIR` and add `--project-dir` to later tools.

2. **memory-audit** (2 tools in parallel):
   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/tools/memory-audit.py" [--project-dir $PROJECT_MEM_DIR] [--json]
   ```
   - `layout` error = flat atoms directly under the global `memory/` root (category folders are required) -> `python "${CLAUDE_PLUGIN_ROOT}/tools/atom-categorize.py" plan` (produces a draft), then `apply --map <json>` to file them; do not mv by hand.
   - For the project layer, `MEMORY.md line count` is only **info** while that project's index still contains flat `shared/<slug>.md` entries (not yet migrated); the line cap (40 lines for the global layer, 150 for the project layer) applies only after migration (`atom-categorize.py plan|apply --memory-dir $PROJECT_MEM_DIR`). The `<!-- atom-catalog -->` block in the project MEMORY.md is maintained by `sync-memory-index.py --write --memory-dir $PROJECT_MEM_DIR`.

3. **atom-health-check** (2 tools in parallel):
   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/tools/atom-health-check.py" --report [--json] [--memory-root $PROJECT_MEM_DIR]
   # if there is a project layer, also run the global layer
   python "${CLAUDE_PLUGIN_ROOT}/tools/atom-health-check.py" --report [--json]
   ```

4. **Effect report** (the effect side beyond structure: were the injected memories actually used?):
   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/tools/memory-effect-report.py"
   ```
   Relay the three lists as they are: A top useful (α/β Wilson lower bound + rescue hits), B high exposure with zero use
   (token tax; with trigger-tightening suggestions), C zero-exposure dead-weight candidates; then the 30-day weekly trend table at the end.
   When B/C have items, point them out, but **do not auto-edit triggers or auto-delete atoms** (the user decides).

5. **Combined report:** format validation / expiry / reference integrity / promotion and demotion / duplicate detection / index consistency. List problems in each block, ✓ if none. End with a summary: N problems / all healthy.

6. **Interaction:** if problems are found, ask whether to fix them. **Never mv an atom across layers by hand**; use `atom-move.py move` or the MCP `atom_move`.

7. **broken_refs -> OFFER L2 self-healing (manual trigger only, zero per-session standing cost):**
   When the report shows **Broken References (dead links)**, **ask** the user whether to run the L2 LLM repair (never run it automatically; Native-first: no long-lived extras).
   If they agree, for each atom with dead links in the report (de-duplicate `broken_refs[].atom`), call the CLI one at a time:
   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/tools/atom-heal.py" --atom <name> --apply --backend ollama --json
   ```
   Calling the CLI directly is the **default path**: headless, no server dependency, no dashboard plugin needed. Read each JSON result:
   - `fixed:true` -> fixed (repointed a typo / removed an invalid link); report what changed.
   - `needs_human:true` -> could not be fixed automatically; `atom-heal.py` has written a diagnostic card to `memory/_heal_review/<atom>.json` -> tell the user to use `/memoryatoms:heal-review` for a manual ruling.

   **Optional fast path** (only when the dashboard plugin is on and confirmed to be running new code): `POST http://127.0.0.1:<dashboard port>/api/heal-all` (the dashboard plugin's port: see its settings) batch-repairs all broken_refs in the background (runs through the server's `healRunner` concurrently, returns 202 `{started,count,pending}`). ⚠️ The live dashboard process may be an **orphaned old process** (`/api/heal-all` is not a new route, so old code will not 404 but silently run the old behavior) -> before using it, confirm the new code is up (restart the dashboard if you are not sure it is the current build); if unsure, use the CLI above.

   One-way links **need no fix**: the reverse direction is derived by the index at injection time (`wg_atoms_pool.load_link_graph`) and is not written into the linked file; this step only heals L2 dead links.

### peek -> view auto-extraction (legacy leftovers only)

> ⚠️ The auto-capture extraction pipeline has been removed (`per_turn` and `session_end_flush` are disabled;
> no longer maintained) and produces no new drafts.
> A result of `{"written":0,"pending":0}` is expected, not a fault. This command only queries old leftovers.

```bash
python "${CLAUDE_PLUGIN_ROOT}/tools/memory-peek.py" $ARGS
```

`$ARGS` is whatever the user passed, such as `--since=...`. Lists atoms auto-extracted in the given period + pending candidates + the trigger reason (currently only legacy leftovers appear).

### undo -> revert auto-extraction (legacy leftovers only)

> ⚠️ Same as peek: the pipeline has been removed, so there are no new writes to revert. `memory-undo.py` still works on old leftovers.

```bash
python "${CLAUDE_PLUGIN_ROOT}/tools/memory-undo.py" $ARGS
```

Supports `last` / `--since=24h` / `--all-from-today`. Reverts atoms written by `auto-extracted-v4.1`, moves them to `_rejected/`, and records the reason.

### review -> self-iteration review

Manually trigger the memory system's self-iteration: decay scan, promotion candidates, utility view.

Automatic self-iteration (promotion / demotion / decay) is run by `_self_iterate_atoms` in the SessionEnd hook; there is no `--self-iterate` CLI option. A manual review runs the same judgments in look-only mode:

1. Detect the project memory directory (same as health step 1).
2. **Promotion / demotion candidates:** `python "${CLAUDE_PLUGIN_ROOT}/tools/memory-audit.py" [--project-dir $PROJECT_MEM_DIR]`; relay the report's "promotion/demotion" and "expiry" blocks (confirmations main track + usefulness Wilson lower-bound track, same source as the hook).
3. **Decay / archive candidates:** `python "${CLAUDE_PLUGIN_ROOT}/tools/memory-audit.py" --enforce --dry-run [--project-dir $PROJECT_MEM_DIR]` (selective forget only lists candidates and moves nothing; to actually move, drop `--dry-run`, after asking the user).
4. **Utility view:** `python "${CLAUDE_PLUGIN_ROOT}/tools/memory-effect-report.py"` (the useful / high-exposure-zero-use / zero-exposure lists).
5. Combine into one table: atom | suggested action (promote / demote / archive / tighten trigger) | basis. **Suggest only, do not execute**; once the user agrees, use MCP `atom_promote` / `atom_edit_meta`, or the tools above without `--dry-run`.

Oscillation detection (the same card repeatedly changed over recent sessions) runs automatically inside the hooks (recorded at SessionEnd, hinted at SessionStart); there is no separate manual entry point.

### score -> session score view

```bash
python "${CLAUDE_PLUGIN_ROOT}/tools/memory-session-score.py" $ARGS
```

Supports `--last` / `--since=24h` / `--top-n=10`. Lists the 5-dimension scores of recent sessions: density / precision / novelty / cost / trust -> weighted_total.

---

## Default behavior

When `$ARGUMENTS` is empty: run `health` directly (same as `/memoryatoms:memory health`). No menu, no questions.
