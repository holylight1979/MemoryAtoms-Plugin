---
name: refile
description: Take any .md file outside memory/_local/, run it through the input guard and the core/config-file guard, propose a filing location with the realm classification engine, then move the file after interactive confirmation and scan for docs that need reference updates. 觸發詞：refile, 歸檔, 手動歸檔, 重新歸位, 歸到應在的地方, 這檔案該放哪, atom 歸類, 移檔歸位, realm 歸檔, 把這個 md 歸位
user-invocable: true
when_to_use: Triggers - refile, file this note, where should this .md go, re-file, move to the atom tree, 觸發詞：refile, 歸檔, 手動歸檔, 重新歸位, 歸到應在的地方, 這檔案該放哪, atom 歸類, 移檔歸位, realm 歸檔, 把這個 md 歸位
metadata:
  pattern: pipeline
---

# Skill: refile (Pipeline)

> File **any `.md` outside `memory/_local/`** where it belongs: three guards → interactive confirmation → move → doc-sync.
> All deterministic judgments live in [scripts/refile_classify.py](scripts/refile_classify.py). **This file only orchestrates the interaction; never re-judge the guards by eye.**

## Triggers

- The user runs `/memoryatoms:refile <path-to.md>`
- "Where should this `.md` go", "file this into the atom tree", "re-file this note" (原話例：「這個 `.md` 該放哪」「把這檔歸位／歸到 atom 樹」「幫我重新歸位這份筆記」)

## Iron rules

1. **Guard verdicts always come from the `verdict` of `refile_classify.py`** (logic over semantics; the LLM must not decide on its own whether a file is a core file).
2. `verdict=core_file` → **never move it**. Report the relations, then offer only two choices: abort / escalate to an architecture-level plan.
3. **Get explicit user confirmation before any move** (this skill is interactive, not automatic).
4. **Always run the doc-ref scan after a move** and list the human-facing docs that need syncing.

## Workflow

### Step 1: Get the target path
- **Action**: take `<path>` from the arguments; if missing, ask the user which `.md` to refile.
- **Done when**: you hold a concrete path string.

### Step 2: Classify (run the guard engine)
- **Action**: `python "${CLAUDE_SKILL_DIR}/scripts/refile_classify.py" classify "<path>"`
- **Done when**: exit 0 and the JSON contains a `verdict` field.
- **Branching**: follow the verdict table below; only `classify` proceeds to Step 3.

### Step 3: Move (**only when `verdict=classify` and the user has confirmed**)
- **Action**: pick the tool by `is_indexed_atom` / `looks_like_atom` / `proposed_realm` (see the move decision table).
- **Depends on**: the Step 2 JSON + user confirmation.
- **Done when**: the target is at its new location (index path updated, or the loose original removed).

### Step 4: Post-move doc-sync
- **Action**: `python "${CLAUDE_SKILL_DIR}/scripts/refile_classify.py" docrefs "<old rel path>"`
- If `hits` is non-empty, list the docs that need syncing and propose a reference fix for each (fix after user confirmation, or mark as a todo).
- **Done when**: every hit is listed with a proposed action.

## Verdict branching

| verdict | Action |
|---------|--------|
| `already_archived` | Already under `memory/_local/` (or the legacy `_AIDocs/_atoms/`, still read until the cards are moved); report "already archived, no refile needed" and stop |
| `not_found` | Report that the file was not found, ask the user to confirm the path, and stop |
| `core_file` | **Do not move.** Summarize using `signals` + `index_doc_hits` + the atom's `Related`: "this is a core/config file of the atom memory system, role = X, related subsystems = Y/Z", then offer two choices: ① **abort** ② **escalate to an architecture-level change plan** (enter plan mode via EnterPlanMode). Stop |
| `classify` | Go to Step 3 (get confirmation first) |

## Move decision table (`verdict=classify`)

| Case | Action |
|------|--------|
| Existing indexed atom (`is_indexed_atom=true`) | `python "${CLAUDE_PLUGIN_ROOT}/tools/atom-set-realm.py" set <stem> --domain "<proposed_domain>"` (done live on the Python side, moves `.access.json` atomically) |
| Loose `.md` with `looks_like_atom=true`, `proposed_realm` ∈ {local, else} | Read the content → create the atom with `atom_write` (`realm=local`, `domain=<proposed_domain>`) → confirm the location → delete the original loose file |
| `proposed_realm=core` | Treat as a cross-project core atom → create it as a **core** atom with `atom_write` (no realm; **`domain="<Lv1>[/<Lv2>]"` is required**: the closed Lv1 list is in `memory/_meta/taxonomy.json`, lands in `memory/<Lv1>/`; for feedback-* titles, domain = failure topic → `memory/Failures/<topic>/`; if no category fits, do not create an atom). If it is already a core atom, leave it and say no move is needed. To move an existing core atom to another category, use `python "${CLAUDE_PLUGIN_ROOT}/tools/atom-move.py" move <slug> --from memory/<old> --to memory/<new Lv1>[/<Lv2>]` |
| `proposed_realm=defer` | LLM unavailable (infrastructure failure) → **do not move**; tell the user to retry later, or ask them to specify `--domain` manually and then use atom-set-realm |
| `looks_like_atom=false` (TODO / transcript / prose) | **Do not put it in the atom tree** → suggest moving it to `memory/_staging/` or `_AIDocs/` (plain `mv`), with user confirmation |

- After any **create/move**, run `python "${CLAUDE_PLUGIN_ROOT}/tools/sync-memory-index.py" --write` to refresh the catalog (the same extra trigger as the SessionEnd sweep).
- If the location `atom_write` reports differs from `proposed_domain`, correct it to the full path with `python "${CLAUDE_PLUGIN_ROOT}/tools/atom-set-realm.py"`.

## Anti-patterns

- ❌ Not running `refile_classify.py` and judging by eye whether a file is a core file
- ❌ Moving a `core_file` anyway (breaks the bootstrap/config chain)
- ❌ Moving a file without asking the user
- ❌ Skipping the doc-ref scan after the move, leaving dead references in docs
- ❌ Forcing a TODO/transcript into the atom tree

## Notes

- `proposed_domain` has already passed `normalize_domain_path` canon (snapped to the existing tree + incremental depth gate, depth=volume); use it as is.
- `Related` between atoms uses the **slug**, so moving a path does not break it; the only risk is **human-facing docs** that cite a path/filename, which the Step 4 scan catches.
- For the full picture of the engine, lexicon self-learning, canon, and the four Fail-safe states, see `skills/refile/scripts/refile_classify.py` (shares one engine with the SessionEnd sweep; this skill is the manual front end).
