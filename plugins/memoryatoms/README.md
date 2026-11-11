# MemoryAtoms

Atomic memory for Claude Code. MemoryAtoms keeps what you and Claude learn as small Markdown cards, puts the cards that fit your prompt in front of Claude on every turn, lets Claude write and search cards through an MCP server, and harvests what a session learned into new cards. It ships with 486 ready-made cards about everyday development work.

繁體中文說明：[README.zh-TW.md](README.zh-TW.md)

- Author: holylight
- License: GPL-3.0-only (see `LICENSE`)

This plugin is the memory core only: writing cards, finding them, and bringing them back when they fit.

## Requirements

- Claude Code with plugin support
- Python 3.10 or newer (hooks and tools are Python)
- Node.js 18 or newer (the hook launcher and the MCP server are Node)
- git: only for project memory under version control, sync, or an organization layer

If Python is missing, the first prompt of each session shows one `[Guardian:Degraded]` line saying what was checked and how to fix it, and memory stays off until then.

## Install and first session

1. Add the marketplace: `/plugin marketplace add holylight1979/MemoryAtoms-Plugin`
2. Install: `/plugin install memoryatoms@memoryatoms`. On Claude Code 2.1.275 or later, steps 1 and 2 are one line: `/plugin install memoryatoms --marketplace holylight1979/MemoryAtoms-Plugin`. The install dialog asks five things:
   - `data_dir` (Data folder): where cards, indexes and logs live. Default `~/.memoryatoms`. Moving from the git install? Enter `~/.claude`.
   - `language`: `en` or `zh-TW` for hints and messages. Cards are never translated.
   - `python`: path to Python 3.10+. Leave empty to find it automatically.
   - `remote_llm_user`, `remote_llm_password`: only if a remote LLM backend you set up needs a login (see "What it sends"). Leave empty otherwise. The password is masked and kept in your system's secure credential store, not in `settings.json`.
   You can change them later with `/plugin configure`.
3. Run `/reload-plugins` or restart Claude Code.
4. The first session start creates the data folder, writes `IDENTITY.md` and `USER.md` from templates, unpacks the 486 shipped cards and builds the index. On the author's machine that first start took about 9 to 17 seconds; later starts take a little over 2 seconds.
5. The first time Claude writes a card, Claude Code asks your permission for the MCP tool. A plugin cannot pre-approve tools.
6. Optional: `/memoryatoms:setup-vector` for semantic search (see Downloads).

Each start also suggests running `/memory` to turn off Claude Code's built-in auto memory. The plugin cannot turn it off for you (it never edits your settings). Reason: with both on, notes are kept in two places, the native one costs about 500 tokens per session, and its rules can contradict the card-writing rules.

## What runs

### Hooks (13 entries on 12 events)

Every hook runs as `node ${CLAUDE_PLUGIN_ROOT}/hooks/run.js <script>.py`. The launcher finds Python (the `python` option, a cached path, then `py -3` / `python` on Windows or `python3` / `python` elsewhere) and runs the script. No hook is attached to Bash or PowerShell. The only tool calls a hook can refuse are Write and Edit on card files written in the wrong format, or on Claude Code's native memory folders; the refusal tells Claude to use the `atom_write` tool instead.

| Event | Timeout | What it does |
|---|---|---|
| SessionStart | 20 s | Builds or refreshes the card index, unpacks or updates shipped cards, sends the session-start cards, re-sends cards after a compaction, prints one-line notices (for example "vector search not set up"). |
| SessionStart | 10 s | Personal start: sends `IDENTITY.md`, `USER.md`, the card catalog and the card-writing rules; creates the two files from templates if missing. |
| UserPromptSubmit | 8 s | Finds the cards that fit your prompt and adds them to Claude's context. |
| PreToolUse (Write, Edit, Agent, Task) | 5 s | Points direct edits of card files to the `atom_write` tool; gives subagents the cards that fit their task. |
| PostToolUse (Write, Edit, Agent, Task, and the MCP tools atom_write, atom_retire, knowledge_harvest_report) | 5 s | Re-indexes edited cards, records card receipts and sources, checks harvest reports, records which cards a subagent received. |
| PreCompact | 5 s | Remembers which cards were in context before a compaction. |
| PostCompact | 5 s | Prepares those cards to be sent again. |
| PostToolBatch | 5 s | Sends them if the session start after compaction did not. |
| Stop | 20 s | Knowledge harvest (asks Claude to save what was learned; blocks at most every second time in a session) and scores which injected cards were useful. |
| SubagentStart | 5 s | Gives cards to subagents that were not started through the Agent tool. |
| SubagentStop | 20 s | Scores which cards a subagent used. |
| StopFailure | 10 s | When a turn ends on an API error, marks it so it is not scored as "cards not used". |
| SessionEnd | 1.5 s (Claude Code's cap for plugin SessionEnd hooks) | Hands the end-of-session work to a background worker and returns at once, so closing is never held up: knowledge extraction, the conflicting-card check, and a vector index refresh if vector search is set up. |

If the git-installed edition of MemoryAtoms is active on the same machine, every hook exits at once and the MCP server registers no tools, so nothing is injected twice. One line at session start explains how to switch (see "Running next to the git install").

### MCP server

One stdio server, `workflow-guardian` (`node ${CLAUDE_PLUGIN_ROOT}/tools/workflow-guardian-mcp/server.js`), with 9 tools:

| Tool | What it does |
|---|---|
| `atom_write` | Create or update a card (checks for duplicates and conflicts, updates the index). |
| `atom_promote` | Raise a card's confidence level. |
| `atom_feedback` | Record that a card helped or misled. |
| `atom_move` | Move a card to another layer or category. |
| `atom_edit_meta` | Change a card's header fields. |
| `atom_retire` | Retire a card. |
| `knowledge_harvest_report` | Report what the session saved, for the Stop harvest check. |
| `memory_search` | Search cards the same way prompt injection does. |
| `atom_source` | Show where a card came from, quoting the original conversation when it still exists. |

To do the work the server starts the plugin's own Python modules on this machine.

### Background work on this machine

- Hooks start the plugin's own scripts: index rebuilds, a follow-up check, a card creation-date backfill, knowledge-extraction workers.
- End-of-session worker: at SessionEnd the hook writes the event Claude Code handed it (session id, transcript path, working folder) to `workflow/se-worker/` in your data folder and starts `hooks/session_end_worker.py` detached from Claude Code, so it keeps running for a while after the session closes. The worker reads and deletes that file and does the end-of-session work; its output is appended to `Logs/session-end-worker.log` in your data folder.
- Read-only git/svn queries (status, log, ls-files, rev-parse, rev-list, check-ignore) in your project and memory folders, only when they are repositories: warnings about index merge conflicts and unpushed cards, card dates.
- If your project contains `.claude/hooks/project_hooks.py`, the session start runs it (`python <file> session_start`, 5 s limit) and shows the lines it returns. This runs code from your project folder.
- On domain-joined Windows machines, the session start runs `whoami /groups` to read your group names, unless a `role.md` file sets your role. The names are mapped to a role (for organization-layer cards) only if you have filled in the group-to-role table (`roles.ad_group_map`) in the config; it ships empty, so by default the command runs but no role comes from it. The names stay on this machine.
- At Stop, only if the session edited the MCP server's own code: a process list (PowerShell `Get-CimInstance Win32_Process`, or `ps`) to tell whether the running server is outdated.
- Local LLM extraction: some workers ask an Ollama server for help: decision extraction at SessionEnd, failure-pattern extraction after you say things like "broken again", the conflict check when a card is written. The default address is `http://127.0.0.1:11434` on this machine. They also need the language model `qwen3:1.7b` (step 4 of `/memoryatoms:setup-vector`, or `ollama pull qwen3:1.7b`). If no Ollama answers there, or the model is missing, extraction does not happen and nothing is sent; it shows nothing on screen, and each failed try writes one `ERROR` line to `Logs/atom-debug.log` in your data folder. Full-transcript extraction at SessionEnd is off by default.
- Vector service: only after you ran `/memoryatoms:setup-vector`. Before that it is never started. Afterwards each session start makes sure a local HTTP service is running, bound to `127.0.0.1` on a port derived from your data folder (28000 to 28999), and starts the local Ollama daemon if it is not running. Turn it off with `vector_search.auto_start_service: false` in your config.

### Tools you run yourself

The skills (`/memoryatoms:memory`, `conflict`, `vector`, `refile`, `org`, `heal-review`, `behavior-preservation` and others) run command-line tools from the plugin folder only when you, or Claude at your request, use them. Two worth knowing: `tools/health-weekly.py` calls `claude -p ... --model haiku` (uses your Claude account), and `behavior-preservation`'s `preserve-check.py` runs the shell command you give it. Nothing schedules these tools in the plugin edition.

One more that touches your repository when you run it: `tools/normalize-eol.py` turns CRLF line endings into LF. On a project memory folder (`--memory-dir`, `--all-projects`) it rewrites the files under `.claude/memory` in place; with `--write-gitattributes` or `--auto` it also runs git (`rev-parse`, `check-ignore`, `check-attr`) and writes a marked block into the repository's `.gitattributes`, or in an svn working copy runs `svn status`, `propget` and `svn propset svn:eol-style LF` on the versioned files there. With `--root` it works on a whole git repository (`git ls-files`, `status`); without `--check` it rewrites that repository's CRLF files and stages them (`git add`; with `--include-dirty` also `git show`, `hash-object -w`, `update-index`). It never commits, pushes or uses the network. The plugin never runs it on its own (see `eol.auto_normalize_project` below); `tools/health-weekly.py` calls it with `--root --check`, which only reads.

## What it reads

| What | Who reads it and why |
|---|---|
| Claude Code transcripts, `<Claude config folder>/projects/<project>/<session>.jsonl` | Stop reads the end of the current session to harvest knowledge and score cards; SubagentStart reads the last tool call; extraction workers read up to 20,000 characters of the current session; `atom_source` opens the older transcript a card came from to quote it. Read only. |
| Claude Code `settings.json` | `autoMemoryEnabled` (for the `/memory` suggestion), `pluginConfigs` (this plugin's options), `enabledPlugins` in user, project and project-local settings (which MemoryAtoms extensions are on), hook commands (to detect the git install). Read only. |
| `~/.claude.json` | `mcpServers`, to detect the git install's MCP server. Read only. |
| `CLAUDE.md` in the Claude config folder | Whether it already imports `IDENTITY.md` (then it is not sent twice). Read only. |
| Your data folder | Cards, indexes, config, state, logs, `IDENTITY.md`, `USER.md`. |
| Your project | `.claude/memory/` (project cards), `.claude/project-tree.json` (project root declarations), `.claude/hooks/project_hooks.py` (see above), and the memory folders of other projects you opened before, for cards marked cross-project. |
| Your OS user name | Fills `USER.md` the first time and keys your personal cards. |

## What it writes

| Where | What | When |
|---|---|---|
| Data folder (default `~/.memoryatoms`) | Cards, indexes, the shipped cards and their record `_shipped.json`, session state, logs, the vector database, `IDENTITY.md`, `USER.md`. | All the time. Kept when you uninstall the plugin. |
| `${CLAUDE_PLUGIN_DATA}` | `python.json` (found Python), `pycache/`, and after setup-vector `venv/` and `downloads/`. | First hook run; setup-vector. Removed on uninstall. |
| `<project>/.claude/memory/` | Project cards and their index, `failures/`, `_staging/`; a `.gitignore` entry (or `svn:ignore` value) for derived index files when the folder is in git/svn. | After you initialise project memory and when a card is written to the project. Exception: when local LLM extraction works, its drafts go to `<project>/.claude/memory/personal/auto/<user>/`, and that folder is created even if project memory was never initialised. |
| Organization-layer folder | Clone, index, organization cards. | Only after you connect one with `/memoryatoms:org`. |
| Claude Code `settings.json` and user MCP registration | Removes the git install's hooks and its `workflow-guardian` MCP registration, after a backup in `<Claude config folder>/backups/`. | Only when you run `/memoryatoms:migrate-to-plugin --apply` (or `--undo`). |

The plugin does not write into its own plugin folder, does not edit `~/.gitconfig`, and does not change `settings.json` or MCP registrations; the only exception is the migration above, which you start yourself (its `--undo` puts the old registration back).

## What it sends

Nothing by default: no telemetry, no analytics, no calls home. Data leaves this machine only if you set one of these up:

| What | Where to | How it gets turned on |
|---|---|---|
| Conversation excerpts, your prompt, card text (the local LLM extraction above) | A remote Ollama-compatible server | Add a backend whose `base_url` is not on this machine under `vector_search.ollama_backends` in `<data folder>/workflow/config.json`. Optional login: user and password (the `remote_llm_user` / `remote_llm_password` options, or the backend's `auth` settings) are sent to the `login_url` you set; the token is cached in `<data folder>/workflow/.ollama_token.json`. https certificates are verified. |
| Your cards (git push / pull) | The remotes your memory folders already use | Turn sync on (`vcs_sync.enabled: true`). Off in the plugin edition. |
| git clone / pull of your organization's memory | The repository URL you configure | Connect an organization layer with `/memoryatoms:org` (off and empty by default). |

## What it downloads

Only `/memoryatoms:setup-vector`. Without `--apply` it only prints the plan. Each step says what it downloads, how big it is and where it goes, and runs only after you agree:

1. Python packages: `lancedb` and its dependencies at pinned versions from PyPI, about 100 to 130 MB download (about 300 MB on disk), into `${CLAUDE_PLUGIN_DATA}/venv`. Your system Python is not changed.
2. Ollama, skipped if already installed: the official installer from the latest Ollama GitHub release, checked against the release's published sha256 (and its Authenticode signature on Windows) before it runs. Windows: about 1.7 GB, silent per-user install into `%LOCALAPPDATA%\Programs\Ollama`. macOS: a 16 KB `install.sh` that downloads the app (about 230 MB) to `/Applications/Ollama.app`. Linux: a 16 KB `install.sh` that downloads Ollama (size not known in advance, roughly 1 to 2 GB with GPU libraries) into `/usr/local` and asks for sudo.
3. The embedding model: `ollama pull qwen3-embedding`, about 4.7 GB, stored by Ollama.
4. The extraction model: `ollama pull qwen3:1.7b`, about 1.4 GB, stored by Ollama. End-of-session knowledge extraction uses it; without it extraction does not run (see "Background work on this machine"). Skip it if you set up a remote LLM.

Without vector search, cards are still found by trigger words and keyword search. On the author's measurements, rephrased questions found the right card 51.8% of the time without vectors and 71.4% with them.

One more case: if you install the `sentence-transformers` library into the vector Python yourself (setup-vector does not), the vector service uses it when Ollama is down, and that library downloads the `BAAI/bge-m3` model from Hugging Face. Set `vector_search.fallback_backend` to `"none"` to prevent it.

## Off by default in the plugin edition

These are written in the plugin's `workflow/config.plugin.json`; your own `<data folder>/workflow/config.json` or `config.local.json` can turn them on.

| Setting | Off means |
|---|---|
| `vcs_sync.enabled: false` | No background git commit, pull or push of any memory folder. Cards stay on this machine. |
| `merge_driver.auto_install: false` | Never edits `~/.gitconfig` to install the card-index merge driver. |
| `skill_index.enabled: false` | No background rewrite of the skill index (it would write into the plugin folder). |
| `eol.auto_normalize_project: false` | Never adds a block to your project's `.gitattributes` or sets `svn:eol-style` on project files. |
| `guard_family.enabled: false` | The coding-guard family (commit gate, cross-layer gates, verification ledger, smell gate and the rest) does not run. It is part of the full edition only. |

Claude Code's built-in auto memory is not something the plugin can switch off; see the `/memory` suggestion above.

## Extensions

Nine optional plugins from the same marketplace. Each one needs `memoryatoms`; installing one also enables it. They are separate so the core stays small: install only what you want.

| Plugin | What it adds | Install |
|---|---|---|
| `memoryatoms-changelog-roll` | Rolls old rows of `_CHANGELOG.md` into `_CHANGELOG_ARCHIVE.md` in the background; `/memoryatoms-changelog-roll:changelog-debug` runs it by hand. | `/plugin install memoryatoms-changelog-roll@memoryatoms` |
| `memoryatoms-codex` | Codex integration: `/memoryatoms-codex:codex` second opinion and build delegation, plus Codex Companion background review. | `/plugin install memoryatoms-codex@memoryatoms` |
| `memoryatoms-coordination` | Multi-session coordination: warns when another live session edits the same file or is about to finish with git (warns only, never blocks). | `/plugin install memoryatoms-coordination@memoryatoms` |
| `memoryatoms-dashboard` | Local web dashboard served from the MCP process: dashboard, brain-world and the wrap-up report HUD. | `/plugin install memoryatoms-dashboard@memoryatoms` |
| `memoryatoms-handoff` | Handoff notes: prepares `next-phase-auto.md` before compaction and at session end, warns when context is nearly full, and provides `/memoryatoms-handoff:handoff`. | `/plugin install memoryatoms-handoff@memoryatoms` |
| `memoryatoms-statusline` | Status line script: edited files, knowledge queue, vector service, wrap-up report (zero tokens). No hooks, no MCP; you turn it on yourself (below). | `/plugin install memoryatoms-statusline@memoryatoms` |
| `memoryatoms-usage-watch` | Claude account usage monitor: quota checks, anomaly notifications, and a session-start hint when the monitor is not running. | `/plugin install memoryatoms-usage-watch@memoryatoms` |
| `memoryatoms-wisdom` | Reflection stats: records how each session went and shows at most two self-awareness lines at session start. | `/plugin install memoryatoms-wisdom@memoryatoms` |
| `memoryatoms-workflow-nudge` | Working-style nudges: suggests fan-out for research-type requests and parallel sub-agents for independent work. | `/plugin install memoryatoms-workflow-nudge@memoryatoms` |

### Status line

Claude Code plugins cannot set your status line, so `memoryatoms-statusline` only brings the script. After installing it, add the `statusLine` setting shown in that plugin's README to your user `settings.json`; remove the key to turn it off.

## Running next to the git install

One machine runs one edition. If the git-installed edition's hooks are in `settings.json`, or its `workflow-guardian` MCP server is registered in `~/.claude.json`, the plugin stays off and says so once per session. To switch, run `/memoryatoms:migrate-to-plugin`:

1. Preview (default): lists exactly which hook entries and which MCP registration would be removed. Changes nothing.
2. `--apply`, after you agree: backs up `settings.json` to `<Claude config folder>/backups/migrate-<time>/`, removes only the git install's own hook entries (other hooks stay), and runs `claude mcp remove workflow-guardian -s user`.
3. `--undo`: restores `settings.json` byte for byte and re-registers the MCP server from the backup.

Cards, indexes, logs, `CLAUDE.md` and your workflow config are never touched. Set the plugin's `data_dir` to `~/.claude` to keep using your existing cards.

## Uninstall

`/plugin` removes the plugin and `${CLAUDE_PLUGIN_DATA}`. Your data folder (`~/.memoryatoms` by default) stays; delete it yourself if you no longer want the cards.

## Machine-readable list

`disclosure.json` lists every item above with when it happens, where, and the file and line in the source that does it.
