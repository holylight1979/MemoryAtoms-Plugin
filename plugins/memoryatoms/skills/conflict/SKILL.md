---
name: conflict
description: "Memory conflict detection and pending-review handling: detect conflicting card (atom) content; the pending subcommands list, approve, or reject drafts held in _pending_review. 觸發詞：記憶衝突, 衝突偵測, 待審, 核准待審, 退回待審, conflict, pending review"
---

# /memoryatoms:conflict — Memory conflict detection and pending review

> Manually trigger vector conflict detection to find atom entries that are semantically similar but contradict each other.
> If the project has its own memory layer, scan it together with the global one.
> The `pending` subcommands handle drafts that the conflict detector blocked at write time and parked in
> `_pending_review/`. This is the **proper way out** after a block; do not use skip_conflict_check.

---

## Usage

```
/memoryatoms:conflict                       # full scan
/memoryatoms:conflict atom-name             # scan conflicts for one atom only
/memoryatoms:conflict --dry-run             # list candidate pairs only, no LLM judgment
/memoryatoms:conflict pending               # list items in _pending_review/
/memoryatoms:conflict approve <target>      # approve a draft -> lands in shared/ (management)
/memoryatoms:conflict reject <target> [reason] # reject and remove a draft (management)
```

---

## Argument parsing

From `$ARGUMENTS`:
- empty -> full scan
- atom name -> `--atom {name}`
- `--dry-run` -> list candidates only, no judgment
- `pending` / `approve <target>` / `reject <target> [reason]` -> go to "Pending review" below and skip the detection flow

---

## Pending review (pending / approve / reject)

Backend: `tools/conflict-review.py` (full list / approve / reject, including the management permission check,
the `.resolved.md` convention, merge history, and reindex). Use the current cwd as `--project-cwd`.

```bash
# List pending items
python "${CLAUDE_PLUGIN_ROOT}/tools/conflict-review.py" --list --project-cwd "$(pwd)"

# Approve (a raw conflict report cannot be approved directly: edit it and save as <name>.resolved.md, then approve <name>.resolved)
python "${CLAUDE_PLUGIN_ROOT}/tools/conflict-review.py" --action approve --target <name> --project-cwd "$(pwd)"

# Reject
python "${CLAUDE_PLUGIN_ROOT}/tools/conflict-review.py" --action reject --target <name> --reason "<reason>" --project-cwd "$(pwd)"
```

Report format: one line per item with `kind / target / author / preview`; for `conflict` items add the hint "resolve first, then approve".
After approve/reject, report dest / removed and the merge history result.
If the result has `note` / `same_card_reports` (other conflict reports for the same card), relay it as "N more pending reports for the same card: ..." and remind the user to review each one before acting; the tool does not close them together.

---

## Step 0: Detect the project memory directory

Run with the Bash tool:

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

If the output is non-empty, record it as `PROJECT_MEM_DIR` and add `--project-dir $PROJECT_MEM_DIR` to later commands.

---

## Step 1: Check prerequisites

Check in parallel:

1. Is the vector service running:
```bash
curl -s "http://127.0.0.1:$(python "${CLAUDE_PLUGIN_ROOT}/tools/memory-vector-service/starter.py" --print-port)/health" 2>/dev/null
```

2. Is Ollama available (needed for LLM judgment):
```bash
curl -s http://127.0.0.1:11434/api/tags 2>/dev/null | head -1
```

- Vector service not running -> suggest `/memoryatoms:vector start` first
- Ollama not running -> use `--dry-run` (list candidates only, no LLM judgment)

## Step 2: Run conflict detection

Run with the Bash tool:

```bash
python "${CLAUDE_PLUGIN_ROOT}/tools/memory-conflict-detector.py" [--atom X] [--dry-run] [--project-dir $PROJECT_MEM_DIR] --json
```

Capture the JSON output.

## Step 3: Interpret results

Parse the detection results. Each conflict group contains:
- **atom_a** / **atom_b**: the two conflicting atoms
- **item_a** / **item_b**: the specific entries
- **relationship**: AGREE / CONTRADICT / EXTEND / UNRELATED
- **similarity**: vector similarity score
- **verdict**: the LLM's explanation

Keep `CONTRADICT` results and any `EXTEND` results worth attention.

## Step 4: Report

Format:

```
## Conflict detection report

Scanned N atoms, found M conflict candidates, K confirmed contradictions.

### Contradictions (CONTRADICT)
1. **{atom_a}** vs **{atom_b}**
   - A: {summary of item_a}
   - B: {summary of item_b}
   - Similarity: {score}
   - Verdict: {verdict}

### Extensions (EXTEND)
{same format}

### Agreements (AGREE) — possible duplicates
{same format}
```

## Step 5: Interactive resolution

For each group, ask the user:
- **CONTRADICT** -> which one is correct? Fix or delete which side?
- **EXTEND** -> merge into one entry?
- **AGREE (suspected duplicate)** -> merge, or delete one?

After confirmation, apply the fix.
