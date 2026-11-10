---
name: migrate-to-plugin
description: Switch this machine from the installed (git) edition of MemoryAtoms to the plugin edition. Previews first, backs up settings.json, removes only the installed edition's hooks and its user-level MCP registration, and can be undone. Cards, indexes, CLAUDE.md and workflow config are not touched. Use when the user wants to move to the plugin, or when the session start says the plugin edition is off because the installed edition is active. Triggers: 切換到插件版, 改用插件版, 遷移到插件.
user-invocable: true
when_to_use: Triggers: 切換到插件版, 改用插件版, 遷移到插件, 從安裝版換成插件, 插件版讓路, 還原遷移, migrate to plugin, switch to plugin edition, undo migration, memoryatoms migrate
metadata:
  pattern: tool-wrapper
---

# /memoryatoms:migrate-to-plugin - installed edition -> plugin edition

> Backend: `tools/migrate-to-plugin.py`. One machine runs one edition: while the installed edition's hooks or its
> `workflow-guardian` MCP are registered, the plugin edition stays off (otherwise every prompt would be injected twice
> and the MCP tools would appear twice). This skill only goes one way: installed -> plugin.

## What it changes and what it never touches

| Changes (backed up first) | Never touches |
|---|---|
| `settings.json` in the Claude config folder (`CLAUDE_CONFIG_DIR`, else `~/.claude`): removes the hook entries that run the installed edition's own scripts (`workflow-guardian.py`, `ensure-mcp.py`, `user-init.sh`, `plan_bash_guard.py`, `lang_guard.py`, `handlers/instructions_loaded.py`, `handlers/stop_failure.py`). Other hooks (extensions such as codex) stay. | Cards, indexes, `Logs/`, `projects/` |
| User-level MCP registration `workflow-guardian` (via `claude mcp remove workflow-guardian -s user`) | `CLAUDE.md`, `workflow/config.json`, `workflow/config.local.json` |
| | The installed edition's program files (nothing is deleted) |

Note for the user: the plugin edition does not ship the code guards (commit gate, cross-layer Bash gate, smell gate, language guard). After switching they are off.

## Steps

1. Preview (changes nothing) and show the user the list:
   `python "${CLAUDE_PLUGIN_ROOT}/tools/migrate-to-plugin.py"`
2. Only after the user agrees to that list:
   `python "${CLAUDE_PLUGIN_ROOT}/tools/migrate-to-plugin.py" --apply`
   The backup goes to `<Claude config folder>/backups/migrate-<timestamp>/`. If `claude` cannot be found, nothing is changed; ask the user for the path of the `claude` command and rerun with `--claude "<path>"`.
3. Relay the printed next step to the user: install the plugin with `claude plugin install memoryatoms@memoryatoms`, and in the install dialog set the data folder (`data_dir`) to `~/.claude` so the existing cards, settings and language keep working. Then restart Claude Code.

## Undo

`python "${CLAUDE_PLUGIN_ROOT}/tools/migrate-to-plugin.py" --undo` restores `settings.json` byte for byte and re-registers the MCP from the latest backup (`--from <backup folder>` picks another one). If `settings.json` changed after the switch, undo stops and says so; `--force` restores anyway and loses those later changes - ask the user first. Uninstalling the plugin is a separate step (`/plugin`).

## Not covered

Plugin edition -> installed edition. Machine-readable output for any mode: add `--json`.
