---
name: atom-debug
description: Toggle the atom memory system's injection/extraction debug log (written to ~/.claude/Logs/atom-debug-*.log). 切換原子記憶系統的注入/萃取 debug log
disable-model-invocation: true
---

# /memoryatoms:atom-debug — Atom memory debug log switch

> Toggles the atom memory system's injection/extraction debug log. When on, the content of every injection and extraction is written to `~/.claude/Logs/atom-debug-{date}.log`.
> Global skill; works in any project.

---

## Usage

```
/memoryatoms:atom-debug
```

No arguments. Each run toggles the on/off state.

---

## Step 1: Read the current state

Read with the Read tool:

```
~/.claude/workflow/config.json
```

Get the `atom_debug` field (boolean, default `false`).

## Step 2: Toggle the state

- Currently `false` (or absent) → set to `true`
- Currently `true` → set to `false`

Edit the `atom_debug` value in `config.json` with the Edit tool.

## Step 3: Report

Reply with the result:

- **On**: "Atom Debug Log is on. Injection/extraction content will be logged to `~/.claude/Logs/atom-debug-{date}.log`."
- **Off**: "Atom Debug Log is off."
