---
name: extract
description: "Manual knowledge extraction: collect knowledge worth keeping from the current conversation, confirm item by item, then write via atom_write."
disable-model-invocation: true
---

# /memoryatoms:extract - manual knowledge extraction

> Collect knowledge points from the **current conversation**; after the user confirms, write them with MCP `atom_write`.
> Global skill, works in any project. Does not depend on Ollama, does not read the transcript, does not call `hooks/extract-worker.py`
> (the worker is hook-only: it finds the transcript from the stdin session_id / cwd; this skill does not use it).

---

## Usage

```
/memoryatoms:extract
```

No arguments.

---

## Step 1: Collect knowledge points from the conversation

Review this conversation (user corrections, pitfalls hit, decisions settled, facts verified) and look in five categories:

- **factual**: factual knowledge (API behavior, tool characteristics, environment differences)
- **procedural**: operating steps, command combinations
- **architectural**: architecture decisions, design choices
- **pitfall**: pitfalls hit, easy-to-get-wrong spots
- **decision**: decisions made and their reasons

**Do not collect**: anything readable directly from the final code / tests / existing docs (function lists, parameter tables, file indexes, line-by-line code). Ask the counterfactual: "If I drop this, will the future repeat the mistake or pay an expensive re-lookup?" If not, do not write it - write a one-line doc path instead. Do not collect speculation the user has not confirmed either.

## Step 2: Realm decision (evaluate every item)

Decide whether the knowledge belongs to **core** or **non-core (local)** - three questions: (1) reusable in 2 or more projects? (2) a system rule, or specific to a single app / tool / environment? (3) stable at the month level, or volatile at the week level?

- **Core** (default): cross-project general knowledge (preferences / decisions / workflows / toolchain / memory-system mechanics) → `atom_write` without realm (default core, lives in `memory/<Lv1>/`, injected into all projects); **`domain` is required** (Lv1 closed list in `memory/_meta/taxonomy.json`).
- **Non-core (local)**: useful only inside ~/.claude (development of a "specific instance" of the memory system / Guardian, the dashboard's brain-world page, pitfalls of specific external tools) → `atom_write` with `realm=local` + `domain` (World/Tools/MemDev); **still scope=global**, automatically filed under `_AIDocs/_atoms/<domain>/`, injected only when cwd is inside ~/.claude, zero burden on external projects.
- Unsure → default core (the safe side; the classifier also defaults safely to core, and the core protection list blocks hard). Mechanism: `skills/refile/scripts/refile_classify.py`.
- Project-specific rules / decisions → `scope=shared`; about the user personally → `scope=personal`.

## Step 3: Present the results

```
## 萃取結果

1. [類型] 知識摘要
   → 建議：append 到 {既有 atom 名稱} ／ 新建 atom（domain=…, realm=…, scope=…）
   → 信心：[臨]

2. ...
```

Check for an existing matching atom using atoms already injected in the conversation, `memory/_ATOM_INDEX.md`, or `_atom_index.json`, comparing Trigger; if similar, append instead of creating.

## Step 4: Write after interactive confirmation

Ask everything in one go: which to write, which atom each goes into (suggestion + user decision), and whether realm / scope follow the suggestion.

After confirmation, call MCP `atom_write` per item (`mode=create` for new, `mode=append` for additions).
- New atoms are **always `[臨]`**: MCP rejects `[觀]` / `[固]` on create (cross-session stability cannot be asserted at first write); promotion comes from later sessions using it or appending, not adjusted here.
- Rejected by the write-gate (duplicate / too long / regenerable content) → follow the rejection message (append to an existing atom, shrink to a conclusion, or drop it); never bypass with `skip_gate`.
- Do not hand-edit `MEMORY.md` / `_INDEX.md`: `atom_write` and hooks maintain the indexes.

Report: how many atoms written / appended, each with its absolute path; list the reasons for any rejected.
