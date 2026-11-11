---
name: continue
description: Read _staging/next-phase*.md and run the saved follow-up task at once (shows a menu when there are several). 觸發詞：續接, 接續任務, continue
disable-model-invocation: true
---

# /memoryatoms:continue — resume a staged task

> Reads the follow-up prompts saved in the staging folder and runs one at once. A lightweight way to continue, for when the next step is already prepared.
> Global skill, works in any project. Several staged tasks can exist side by side; you pick one.

---

## Usage

```
/memoryatoms:continue
```

No arguments. When there are several tasks, a menu is shown; reply with a number.

---

## Step 1: Scan the staging folder

Find the staging folder from the "Additional working directories" in the system context, or from the CWD, and scan it with the **Glob tool**:

```
{project_root}/.claude/memory/_staging/next-phase*.md
```

> The staging folder is `{project_root}/.claude/memory/_staging/` (the project's own memory layer).
> Older projects that have not migrated: `~/.claude/projects/{slug}/memory/_staging/next-phase*.md`
> Example: CWD `<project root>` -> scan `<project root>\.claude\memory\_staging\next-phase*.md` first.

Every project has its own staging folder, so follow-ups of different projects never interfere.

### Branching

- **1 file found** -> pick it automatically and go to Step 2.
- **Several files found** -> list them and let the user **pick a number**:

```
_staging/ holds N pending tasks:
  1. build-pipeline — [follow-up] packaging pipeline (Phase 1)
  2. token-diet — [follow-up] token slimming plan

Pick one (enter a number):
```

> The name in the list is the `{name}` part of the file name `next-phase-{name}.md`; the summary is the file's first line.

- **0 files found** -> reply "No pending task (no `next-phase-*.md` under `_staging/`)." and stop.

## Step 2: Read, then delete

1. Read the whole selected file and remember its content.
2. **Delete the file immediately** (so it cannot run twice).

## Step 3: Execute (read back first, then start work)

1. **Read-back (closed-loop confirmation)**: the **first output** after deleting the file (before any other tool call) must be a plain-language restatement for the user, covering:
   - **My understanding**: what this follow-up task is about and how far it has got (in your own words, not a paste of the original).
   - **Work about to be done**: the concrete steps you plan to take next.
   - **Key constraints / open questions** (only if there are any).

   Purpose: the user sees your understanding right away and **can correct it immediately if it is off**. This step is **non-blocking**: after saying it, go straight on to the next step without waiting for confirmation. The file is already deleted, so this restatement is also the only trace of its content. Skipping it or squeezing it into one line breaks this skill.
2. If the restatement turns up a **contradiction / gap / doubtful assumption** in the document (check it against the handoff-writing rules: a handoff must carry the whole picture, with nothing lost or distorted) -> **ask for clarification first; do not follow it blindly**. If there is no doubt, continue.
3. Treat the content you read as the **task prompt** and start executing at once, with no extra confirmation.
4. **Note**: if the task's completion condition includes "produce the next phase's follow-up prompt", then when the task is done write a new `next-phase-{name}.md` (keeping the original task name).
