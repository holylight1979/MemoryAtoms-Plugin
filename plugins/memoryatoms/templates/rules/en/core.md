# Core rules

> This file holds only the rules you need to know beforehand, or that no program gate enforces; what the programs already block or prompt for is not repeated here.

## How the system sees itself
- Goal of the memory system: keep every piece of knowledge and experience, filed by category, delivered in full the moment it is needed, and costing no tokens when it is not relevant. When the two conflict, getting the needed knowledge delivered comes first and saving tokens second.
- **Fine granularity, solid indexes** (attend to and practice this routinely): split memory, cards and documents alike, into finer-grained pieces, and build effective indexes for them. The payoff is precise targeting: the LLM can find an entry point and choose what to read, and scripts can inject by fixed logic at fixed moments.
- **Three memory layers** (scope: who a card is for, decided when it is written): the root layer `~/.claude/memory/` holds what applies across projects → `global`; the organization layer (an organization memory repo, once connected) holds what the whole organization shares → `org`; the project layer `{project}/.claude/memory/` holds that project's decisions and pitfalls → `shared`／`personal`.
- **Change the root only from the root**: system files under `~/.claude` are changed only in a session opened in `~/.claude`; a project session that runs into one writes the need as a ready-to-paste prompt for the user. Running a root tool is not changing it: run the memory system's `tools/<tool>.py` by absolute path (the hook's prompt line gives the full command); do not `cd` into it.
- **Observability is non-negotiable**: a failure that does not block (fail-open) must still be reported — degradation or a silent failure always surfaces a signal (warning, stderr, end-of-task report); never swallow it.

## Knowledge base
- Never change code from memory; details in [[feedback-未實證先別斷言-從根源驗證-先證再修-反退避反冗長]].
- `_AIDocs` (the project's document library) holds only long-term reference; plans, TODOs and work in progress go in `memory/_staging/`.
- Code, config, tests and cards describe only the current state, with no versions, dates or change stories; history goes in `_AIDocs/DevHistory/` and `_CHANGELOG.md`.
- Writing rules or decisions: only the goal is written as "always"; a method is written as "currently uses X"; a rollback or rejection says whether the direction was wrong or the timing or resources were not there.

## Memory
- The user says "初始化原子記憶庫" (initialize atom memory) or `[Guardian:ProjectMemory]` appears at session start → run the command the prompt line gives; do not create the folders by hand.
- Cards are currently written through the atom_write MCP tool (it validates, dedups and indexes); do not edit a card .md directly. A new card currently starts at [臨] and is promoted by use or append. create needs a `domain` (list in `memory/_meta/taxonomy.json`); unsure where it lands → `dry_run` first; knowledge that fits no category is not written. Cite recorded facts directly; do not record temporary attempts or unconfirmed guesses.
- **Scope** asks "does it follow the project or the person?": project rules and decisions → `shared`; only about this person → `personal`; this person's preferences across projects → `personal, cross_project=true`; every project in the organization should know → `org`.
- **Realm** (which area a card lives in): useful across projects → `memory/<category>/`; useful only inside `~/.claude` → `realm=local`. Three questions: reusable in two or more projects? A system rule, or about one app, tool or environment? Valid for months or for weeks? Leaning to the first → core, to the second → local; unsure → core.
- **Personal sensitive information goes in secret** (passwords, tokens, private keys, credentials, personal data, internal addresses, anything not meant to be public): call `atom_write` with `secret=true`; the card says where the value lives, never the value itself. Only general technical knowledge → pass `not_sensitive=<reason>`; unsure → treat it as sensitive (moving it out of secret later is easy; once public it cannot be taken back). A card that is only partly private is split into two; a card filed in the wrong area is moved with `atom_move`.
- **Lists do not go into cards**: todos, backlog, audit results, lookup tables — anything that keeps growing — live in `memory/_staging/<topic>.md`, and the card holds only a pointer; cards are injected whole within a budget, so long lists get cut. When a card is refused for size, first tell whether it is a conclusion or a list: compress a conclusion, reshape a list; do not make room by deleting existing content.
- A prohibition in a Failures card (a pitfall record) carries the marker "情境：" (context; kept in Chinese because the write gate looks for it) on the same or the next line, saying when it applies and when it does not.
- An injected card clearly saved you or misled you → cast a vote with `atom_feedback(atom_name, verdict=helpful|misleading)`; do not vote on cards you did not actually use.

## Version control
- Memory cards and indexes are uploaded by the system in the background (if you turned sync on); the AI does not touch them or ask.
- Code and document changes wait for the user's command (including the memory system's own): when done, report only what changed and what was verified, and let the user review the diff and give the version-control command (by default "上GIT"; the list is `commit_order.keywords` in `workflow/config.json`) before commit＋push.

## Conversation
- /resume → /memoryatoms:continue.
- An independent subtask may go to a new conversation; save the knowledge before splitting. When the conversation is about to be compacted, remind the user to start a new session.
- In Plan Mode, reading currently uses the Read / Grep / Glob tools, not Bash cat/grep/sed; Bash commands use absolute paths with no `cd` prefix.
- Medium or large work that splits cleanly (at least two parts that do not write the same files, each reading many files or taking several minutes) goes to parallel agents on your own initiative, without asking first; each part names its model, see the parallel-dispatch skill. How to split a research request: [[workflow-research-fanout]].
