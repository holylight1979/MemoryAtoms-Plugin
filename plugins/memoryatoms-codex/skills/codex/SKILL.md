---
name: codex
description: Ask Codex for a second opinion (read-only review) or hand it a build job (edits files in a dedicated worktree), after filling a sync package so its overview matches CC's. CC decides when to call it - when a wrong conclusion is costly (design direction, many files affected, irreversible), when CC already has an answer to a design discussion and wants another context to challenge it, when boundary counter-examples are needed, or when a well-bounded job with writable acceptance criteria can be handed off in parallel; also when the user says "加派 codex", "問 codex", "叫 codex 審", "交給 codex 做". Do not use for docs, comments, or wording only; do not use when the answer is already found and only the user's confirmation is missing. Toggling the old Codex Companion supervision switch is /memoryatoms-codex:codex-companion, not this.
user-invocable: true
allowed-tools: Bash(python *codex-run.py *)
when_to_use: 觸發詞：加派 codex, 問 codex, codex 第二意見, codex 審查, 叫 codex 審, 交給 codex 做, codex 施工, codex agent, 同步包, codex-call
metadata:
  pattern: pipeline
---

# Skill: codex (Pipeline)

> Keep Codex's overview of the situation almost identical to the current session's CC, then ask it for an independent judgment or a build. [design choice] Codex's overall picture should be almost fully in sync with the current session's CC, instead of a narrow spot check.
> CC decides whether to call; there is no hook gate. Each rule carries its source: [design choice] = a deliberate rule of this skill; [evidence] = happened in the records; [inference] = AI-derived, not a set rule.

## 1. Whether to call

Call when:
- A wrong conclusion is very costly: design direction, many files affected, irreversible. [design choice] Call Codex only when the overall picture makes a serious problem very likely.
- In a design discussion, CC already has its own answer and needs another context to challenge it. [design choice] Thinking and design discussion inside a session should always bring Codex in.
- Boundary counter-examples are needed; this is Codex's strength. [evidence] in cross-model review its BLOCK findings held up.
- A well-bounded piece of work with writable acceptance criteria can be handed off and done in parallel. [inference]

Do not call when:
- Only docs, comments, or wording change. [design choice] Codex's opinions on documents are not adopted.
- The answer is already found and only the user's confirmation is missing. [inference]

The decision does not depend on how many files or lines changed. [evidence] The old gate used "4+ files or 150 lines" and was tripped by nine one-off scripts.

## 2. Flow (do not enter the next step until the previous one passes)

Script: `python ${CLAUDE_SKILL_DIR}/scripts/codex-run.py` (subcommands `new` and `run`; `--help` lists all parameters). Put temporary files in the session scratchpad.

### Step 1: Write your own answer first
- Review mode: the conclusion CC intends to give the user, or the explanation of this design/change, is section 7.
- Build mode: the work order (what to do, acceptance, what may and may not be touched, verification list with sentinel lines) is section 7.
- Done when: section 7 is written.

### Step 2: Generate the prompt skeleton
- `codex-run.py new --mode review|build --out <scratchpad>/prompt.md`
- The script pastes the ruler of section 2 verbatim (IDENTITY "conflicts and code requirements", coding-style priority plus "design trade-off ruler", code-thinking in full); CC does not copy it by hand. [inference] Codex's own `~/.codex/AGENTS.md` lacks this ruler and ranks "minimal diff" very high, so it leans toward the status quo on design questions.
- Done when: last line is `CODEX_RUN_NEW DONE`.

### Step 3: Fill the sync package honestly (each section says how inside it)
- Section 1: copy the user's original words verbatim; do not put CC's own added limits here. [evidence] In one real case the first round was steered wrong, mainly by a self-added limit ("do not change the save format") slipped in during hand-off.
- Section 3: facts, inferences, unknowns only; conclusions and suggestions go only in section 7. [inference] Putting them in section 3 defeats the "form your own conclusion before seeing section 7" step.
- Section 5: paste the knowledge text of the cards verbatim, not a list of card names. Codex has no card auto-injection, and picking from the index by itself is inaccurate; it keeps its own memory (`~/.codex/memories/`), which is not synced with ours.
- Section 6: give repo paths so it reads them itself, not just a diff; list the auto-loaded CLAUDE.md paths too. [evidence] Every recorded misjudgment was the "did not see beyond the diff" type: diff only, review after commit gave an empty diff, untracked new files absent from the diff, masked paths.
- Done when: no "〔待填" remains in the file (`run` refuses otherwise).

### Step 4: Run
- Review: `codex-run.py run --mode review --prompt-file <prompt.md> --cwd <repo> --out-dir <scratchpad>/<topic> [--guard <other repos it may read>]`
- Build: create the dedicated worktree and branch first, then `codex-run.py run --mode build --prompt-file <prompt.md> --cwd <worktree> --out-dir <...> --guard <live repo>`
- The script fixes the sandbox by mode (review read-only, build workspace-write). Without it Codex follows the `danger-full-access` default in `~/.codex/config.toml` and can write to live. [evidence] confirmed by reading the config.
- The script records git/svn status of the guard before and after the run and exits 1 on any change. [evidence] This guard is what caught a read-only sandbox that failed to start.
- If the read-only sandbox cannot start: note the original error, copy the repo into the scratchpad with `git archive` (svn: `svn export`) or a plain copy, and rerun once with `--sandbox workspace-write --cwd <copy> --guard <original repo>`. Do not use `danger-full-access`. [inference]
- Not tested: whether pytest has a writable temp dir inside the workspace-write sandbox; under the read-only sandbox pytest aborted entirely. [evidence]
- Done when: last line is `CODEX_RUN DONE … ok=1 guard_changed=0`; raw output is in `<out-dir>/codex-output.md`, Codex's own execution log in `codex-stderr.txt`.

### Step 5: CC rules on the result (not delegated to Codex)
- For every point, first ask which original sentence it serves (section 1, or the ruler in section 2). One that cannot point to any is drift: write one line of reason and do not act on it. [design choice] A point the LLM judges to be drift is simply not done.
- If Codex answered very briefly or said "no problem": check `codex-stderr.txt` to confirm it actually read the files before adopting. [evidence]
- If it asks to change or shrink the goal without proof of "cannot be done" (only cost), do not adopt. [inference] Per code-thinking "only one case may propose changing the goal".
- Turn "severe" into a counter-example and reproduce it yourself: if it holds, fix on the spot (write the test red first, fix to green); if not, write which line of code blocks it. [evidence] second-opinion disagreement ruling card.
- Do "quick and effective" fixes immediately. [design choice] When a point is relevant and the problem is real, fix it right away.
- A rejection cannot be just "our tests are all green"; an adoption cannot be just "Codex said so". [evidence]
- Do not open a second round to hunt for new holes; the second round only confirms that the adopted points are fixed. [evidence] Adversarial review has no cap: one review went twelve rounds and Codex found a new corner each time.
- Build mode: CC reads the diff itself, reruns the verification list in the worktree itself (the pass count is CC's), and commits only after checking against the original words. [evidence] Codex's sandbox environment may not be able to run them.
- Reply to the user in three parts: CC's answer, Codex's answer and the disagreement, CC's ruling with reasons. [design choice]
- Done when: every Codex finding has "adopt / not adopt + reason".

## 3. Cards to read before calling (the trigger brings them in automatically)

- The main session is not led by codex, drift found by review is not done, second-opinion disagreement ruling, judge an AI analysis by its conclusion first

## Files

- `assets/sync-package.md`: sync package sections 1-6 (shared by both modes)
- `assets/review-prompt.md`: review mode (section 7 = draft conclusion, two severity levels, red flags, stop condition)
- `assets/build-prompt.md`: build mode (section 7 = work order, conditions to stop and report)
- `scripts/codex-run.py`: `new` builds the skeleton and pastes the ruler; `run` refuses unfilled marks, fixes the sandbox, compares before/after state, saves raw output
