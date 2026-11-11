---
name: fix-escalation
description: "Fix Escalation Protocol — when a fix keeps failing, do root-cause investigation plus independent verification first, and escalate to a multi-agent meeting only if the evidence conflicts. 觸發詞：精確修正, 升級協議, 反覆修不好, 修了又壞, fix-escalation"
disable-model-invocation: true
---

# Fix Escalation Protocol

> Triggers automatically from the 2nd fix attempt on the same problem (Guardian injects `[Guardian:FixEscalation]`), or manually with `/memoryatoms:fix-escalation`.
> Escalate by evidence: **Stage 1 root-cause investigation + independent verification (at most 2 agents) -> if the evidence is consistent and reproducible, fix directly; only if it conflicts or cannot be reproduced, go to the Stage 2 meeting.**

## Problem description

$ARGUMENTS

## Instructions

Main AI role: **user-intent-oriented, precise problem fixer.** Ask the user wherever unsure, and synthesize all evidence into the final decision.
Launch each agent with the Agent tool; give each agent only the context it needs, not the whole conversation.

---

### Stage 0 — Pause and take stock

Tell the user: "⚠ Entering precise-fix mode — pausing direct fixes to find the root cause first."

Record:
- The problem description ($ARGUMENTS or the current problem)
- What each earlier fix attempt changed and what happened (attempt 1, attempt 2, ...)
- Error messages / symptoms / reproduction steps (if there are no repro steps, ask the user or find them yourself)

---

### Stage 1 — Root-cause investigation + independent verification (at most 2 agents, **in parallel in a single message**)

**Agent A (root-cause investigator) prompt:**
```
You are the "root-cause investigator". Task: find the root cause of the problem below; do not propose a fix.
1. Glob/Grep for related code (definitions, callers, types, config)
2. Read _AIDocs/ (locate via _INDEX.md first) and related atoms
3. Use git log -20 / git blame to find where the problem line came from and recent changes
4. Trace the data flow: from the symptom backward to the first value/state that is not as expected, with file:line

Output:
## Root-cause report
### Symptom -> first abnormal point (file:line + evidence)
### Why earlier fixes did not work (compare against each attempt)
### Unconfirmed hypotheses (each with one check that can tell them apart)

Problem: {problem description}
Tried: {what each fix changed and the result}
Symptoms: {error messages / repro steps}
```

**Agent B (independent verifier, does not see Agent A's conclusion) prompt:**
```
You are the "independent verifier". Task: assume no cause; actually reproduce the problem and pin down the trigger condition.
1. Run the repro steps once (test / script / command) and record the actual output
2. Change one variable (input / environment / version / timing) and run again; find the smallest difference that makes the symptom appear and disappear
3. Look up external information only when local evidence is insufficient (WebSearch <=2 keywords, WebFetch <=2 pages), and cite the source

Output:
## Verification report
### Reproducible? (steps + actual output)
### Minimal trigger / disappearance condition
### Comparison with known external information (list if any, otherwise write "not checked")

Problem: {problem description}
Repro steps: {steps}
```

**Decision gate (main AI)** — once both reports are in, ask only one thing: **is the root cause single, do both sides' evidence agree, and is it reproducible?**
- **Yes** -> go straight to Stage 3 (fix). No meeting.
- **No** (the two sides point to different causes / cannot reproduce / the root cause is in an external system you do not know well) -> go to Stage 2, and write the "point of conflict" as one sentence for the meeting topic.
- Information insufficient but direction clear -> run one targeted extra check only (yourself or one more agent); do not rerun all of Stage 1.

---

### Stage 2 — Meeting (only when evidence conflicts or cannot be reproduced)

Launch 3 agents at once (**in parallel in a single message**). Input = the two Stage 1 reports + the point of conflict written by the main AI:

- **Proposer**: from the evidence, propose a root-cause hypothesis and fix (with code snippets), the expected effect, and confidence with reasons
- **Critic**: blind spots and risks of each proposal, alternative routes the proposer missed, worst case
- **Implementer**: for each proposal, feasible / risky / infeasible and why, priority order, prerequisites and side effects; **do not quantify a "hit rate", qualitative analysis only**

The main AI synthesizes: adopt consensus directly; if disagreement remains, run one more round **on the disagreement only** (at most 2 agents: proposer + critic, each responding point by point to the other), without rerunning all three. If the disagreement is a business trade-off, ask the user.

---

### Stage 3 — Fix and wrap-up

1. Brief the user: root cause in one sentence, the fix, and the scope of impact; execute once there is no objection.
2. The fix **touches only the root-cause path**; note other problems found along the way, do not fix them in the same change.
3. Self-check before finishing (the main AI does this itself, no agent): compare `git diff --stat` against the intended scope; clean up leftover debug output / temp files / unused imports / TODO comments on the spot.

---

### Stage 4 — Verification

1. Rerun the Stage 1 repro steps: the symptom is gone and the paths that worked before still work (test / build / manual check, with output).
2. **Success** -> report: "Precise fix ✓ — root cause: ...; verification: ..."
3. **Failure** -> return to the decision gate: count the new evidence from this verification and re-decide whether Stage 2 is needed; tell the user where it is stuck and what was tried.
4. **3 consecutive** precise fixes without a resolution -> force a pause and realign fully with the user (is the problem definition right, should the approach change).
