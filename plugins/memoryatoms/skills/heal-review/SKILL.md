---
name: heal-review
description: Adjudicate the memory self-heal failure queue (management role) - review the diagnostic cards under _heal_review/ for atoms that could not be fixed automatically, then resolve or dismiss them. 管理職裁決記憶自癒失敗佇列：審視 _heal_review/ 下「自動修不好」的 atom 診斷卡並 resolve/dismiss
disable-model-invocation: true
---

# /memoryatoms:heal-review — Memory self-heal failure queue adjudication

> `atom-heal.py` repairs broken memory automatically (L1 mechanically adds missing backlinks / L2 uses the LLM to fix dead links).
> When it cannot fix one (validation fails / needs_human), it leaves a diagnostic card at `memory/_heal_review/<atom>.json` and hands it to a human.
> This skill guides the adjudication: fixed → `resolve` clears the card; decided not to fix → `dismiss`. Who may resolve/dismiss is set by `review.deciders` in `workflow/config.json` (empty = anyone may adjudicate).

---

## Usage

```
/memoryatoms:heal-review                      # list the human-review queue
/memoryatoms:heal-review list
/memoryatoms:heal-review show <atom>          # view one card (what is broken / LLM proposal / failure reason)
/memoryatoms:heal-review resolve <atom>       # fixed → clear the card (rescans first to confirm health; --force if still unhealthy)
/memoryatoms:heal-review dismiss <atom>       # won't fix → clear the card (won't-fix)
```

Backend: `python "${CLAUDE_PLUGIN_ROOT}/tools/heal-review.py" <action> [atom] [--force] --json`

---

## Step 1: List the queue

Run with Bash and list the cards awaiting human review to the user:

```bash
python "${CLAUDE_PLUGIN_ROOT}/tools/heal-review.py" list --json
```

Each card has `atom` / `level` (L1|L2) / `broken_after` (dead links still unresolved) / `proposals` (the LLM's judgment at the time).
If `count=0` → tell the user there are currently no self-heal failures awaiting human review.

## Step 2: Look at details (as needed)

```bash
python "${CLAUDE_PLUGIN_ROOT}/tools/heal-review.py" show <atom> --json
```

Explain to the user the **root cause** of why this atom could not be fixed automatically (usually the dead link's target needs to be created, or the LLM could not decide whether to repoint or remove), and give a **fix recommendation** (what each [[link]] should become / whether to delete it / whether to create the target atom).

## Step 3: Manual fix (if fixing)

Fix the atom as recommended (through the funnel):
- Change Related/Trigger → MCP `atom_edit_meta`
- Add knowledge/sections → MCP `atom_write mode=append`
- Need to create the target atom → MCP `atom_write mode=create`
After fixing, go to Step 4 and clear the card with `resolve` (it rescans to confirm).

## Step 4: Adjudicate and clear the card

```bash
python "${CLAUDE_PLUGIN_ROOT}/tools/heal-review.py" resolve <atom> --json   # fixed (rescan must be healthy, otherwise --force)
python "${CLAUDE_PLUGIN_ROOT}/tools/heal-review.py" dismiss <atom> --json   # decided not to fix
```

`resolve` rescans with `atom-health-check --atom <atom>`; if problems remain it blocks (unless `--force`).
Actions are written to `memory/_merge_history.log` (action=`heal_resolved` / `heal_dismissed`).

---

## Notes
- Who may resolve/dismiss: `wg_roles.is_management()` reads `review.deciders`; an empty list = anyone may adjudicate. To restrict, fill in a list of AD account names; no declaration file is needed.
- Diagnostic cards are JSON (not atom format) and do not go through the atom funnel; clearing a card = deleting that JSON.
- For self-heal mechanism details see `tools/atom-heal.py`.
