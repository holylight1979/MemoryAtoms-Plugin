---
name: org
description: One-stop entry for the org layer (scope=org) of shared memory: connect the org memory repo, check connection and identity/role status, write knowledge to the org layer, and register skills/MCP/project tools as tool cards. Use when the user says things like "connect org memory", "save this to the org layer", "register tools", "what's my role". 觸發詞：接上組織記憶, 記到組織層, 登記工具, 我現在是什麼職能
user-invocable: true
when_to_use: Triggers - org, org layer, org memory, connect org memory, tool cards, register tools, my role. 觸發詞：接上公司記憶, 公司記憶, 公司層, 記到公司層, 登記工具, 工具卡, 我的職能, org, 接上組織記憶, 組織記憶, 組織層, 記到組織層
metadata:
  pattern: tool-wrapper
---

# /memoryatoms:org — Org-layer shared memory

> The org memory repo is a memory layer that every project can see and write to. This skill wraps four tasks into one entry
> so the user can just ask in plain words, with no commands to remember. For mechanism details see `TECH.md` §4.4 in the install folder; for script arguments,
> `python "${CLAUDE_PLUGIN_ROOT}/tools/org-memory.py" --help` is authoritative.

## Usage

```
/memoryatoms:org                 # default: status
/memoryatoms:org status          # connected or not, how many cards, sync state, who I am / my role
/memoryatoms:org join [path]     # connect a new machine in one step (clones automatically if not cloned)
/memoryatoms:org scan            # register skills / MCP as tool cards
/memoryatoms:org scan <project root>   # also scan that project's .claude/tools/*.py
```

There is no subcommand for writing org knowledge: call `atom_write` directly with `scope` set to `org`.

## Intent → action

| User says | Do |
|---|---|
| The startup line has `❓ [Org] The organization has a memory layer … not connected to it yet and the user has not been asked` (zh-TW installs: `❓ [Org] … 還沒接上，使用者也還沒被問過`) | Before the first reply, ask with AskUserQuestion: use the default path (Recommended) / specify a path / not now; then run `--join [path]` or `--decline` according to the answer |
| Connect org memory / why is there no org layer / `[Org] Org-layer memory is not connected` (zh-TW: `[Org] 尚未接上`) | `python "${CLAUDE_PLUGIN_ROOT}/tools/org-memory.py" --join [path]` |
| Don't connect the org layer on this machine / stop asking me | `python "${CLAUDE_PLUGIN_ROOT}/tools/org-memory.py" --decline` |
| Org layer status / what is my role now / who can adjudicate | `python "${CLAUDE_PLUGIN_ROOT}/tools/org-memory.py" --status` |
| Save this to the org layer / the whole organization should know this | `atom_write(scope="org", domain=<Lv1>, …)` |
| Register tools / scan tool cards | `python "${CLAUDE_PLUGIN_ROOT}/tools/org-memory.py" --scan-tools [--project <project root>]` |
| Does the org layer have a tool/knowledge about X | `memory_search(query=…)`; results whose `scope` is `org` are from the org layer |

## Completion criteria and reporting per action

### join
- Success: the output contains `[org-memory] done:` (zh-TW: `完成：`), exit 0. Report in one sentence that it is connected, and that after restarting Claude Code the startup line will show `[Org] Org layer: N atom(s)` (zh-TW: `[Org] 組織層 N 顆`).
- No path needed: the location comes from `org_memory.default_root` in the shared config; the connection state is written to the local `workflow/org-memory.local.json` (not version-controlled), so it does not dirty the `~/.claude` working tree.
- Exit 2 `no local path` / `nothing to clone` (zh-TW: 「沒有本機路徑」/「無從 clone」): the config has no `default_root`/`repo_url` and no path was given → ask the user for the org memory repo's URL or local path, and ask only that.
- Exit 1 `clone failed` (zh-TW: 「clone 失敗」): report the tail of stderr verbatim (usually repo permissions or network); do not switch to another way of forcing the connection.

### status
- After reading the JSON, answer in plain words in three sentences: whether the org layer is connected and how many cards; sync state (`uncommitted`/`ahead`/`behind` all 0 means synced); identity and role source (`roles_source`: `ad` = AD group, `role.md` = manual override, `none` = not found).
- If `ready` is false → go straight on to join, without asking back.
- Wrong role: override manually with `python "${CLAUDE_PLUGIN_ROOT}/tools/init-roles.py" --project-cwd <project root> --me <role>`; delete that `role.md` to fall back to AD resolution.

### Writing org knowledge
- Test: "If I switch to another project and another person, does this still hold?" Write to org only if it does; if it holds only for this project, write `shared`; if it concerns only this person, write `personal`.
- `domain` must be an existing Lv1 of the org layer (`domains` in `<org layer root>/.claude/memory/shared/_taxonomy.json`); only when none fits, open a new Lv1 with `allow_new_category=true` and state in the report what was opened.
- No manual commit needed after writing: the background vcs-sync commits and pushes org-layer changes.

### scan
- Create only the missing cards and leave existing ones alone; cards whose entry-point file has disappeared are marked `Status: deprecated` (not deleted).
- Report "how many created, how many retired"; list every `Skipped` or `Failed` line in the output verbatim (zh-TW: 「略過」/「失敗」).

## Iron rules

1. Always write org-layer files through `atom_write`/`org-memory.py`; never edit atom files in the org repo directly.
2. Do not decide "who can adjudicate" for the user: change the `review.deciders` list only when the user says so explicitly.
3. If a script fails, report stderr; do not work around it, and do not retry more than three times.
