---
name: codex-companion
description: Toggle the Codex Companion supervision system (GPT second-opinion audit). 切換 Codex Companion 監督開關。
disable-model-invocation: true
---

# /memoryatoms-codex:codex-companion — Codex Companion switch

> Toggles the Codex Companion supervision system. When on, Codex (GPT) gives an independent second opinion at plan review, turn audit, and acceptance judging (the last also needs the coding gate on); turning it off stops all three.
> Global skill, works in any project.
>
> **Handoff self-check**: Codex review of handoff files is controlled by `soft_gate.handoff_review` or `handoff_review_v6` (which one applies depends on the coding gate); both are off by default, so turning this switch on does not start it. Handoffs rely on the skills/handoff Step 3.5 self-review; for a second opinion, call skill `codex`.
>
> **Architecture**: this skill only flips a config flag and does not manage a service lifecycle; the Codex audit runs as a short-lived subprocess `tools/codex-companion/audit.py` (in-process state), with no resident daemon.

---

## Usage

```
/memoryatoms-codex:codex-companion
```

No arguments. Each run toggles the on/off state.

---

## Step 1: Read the current state

Use the Read tool on:

```
~/.claude/workflow/config.json
```

Get the value of `codex_companion.enabled` (boolean; the config ships with `true`, and the program treats a missing key as `false`).

## Step 2: Toggle

- Currently `false` (or missing) -> set to `true`
- Currently `true` -> set to `false`

Use the Edit tool to change `codex_companion.enabled` in `config.json`.

## Step 3: Report

Reply with the result:

- **On**: "Codex Companion is on. Plan review, turn audit, and acceptance judging will spawn the short-lived `audit.py` subprocess for Codex's second opinion."
- **Off**: "Codex Companion is off."

No cleanup needed - it takes effect at the next hook trigger, and there is no resident daemon.
