---
name: parallel-dispatch
description: For medium-to-large work that can be split, decide on your own to run several agents in parallel, covering how to split, which model each piece gets (Fable / Opus / Sonnet / Haiku), how to write work orders so agents do not collide or drift, and how to reconcile what comes back. Decide without being asked, and use it when at least two pieces write different sets of files and each piece reads many files or takes several minutes. Also use it when the user says to open more agents, run in parallel, divide the work, or speed things up. Not for a small single-file edit or anything one or two commands can answer. Triggers - 多開 agent, 並行, 平行處理, 分工, 派工, 拆 agent, 加速完工, 巢狀 agent, parallel agents, fan out.
user-invocable: true
when_to_use: Triggers - parallel agents, fan out, split the work, divide the work, speed this up, run in parallel, nested agents, multi-agent, dispatch agents, 多開 agent, 並行, 平行處理, 分工, 派工, 拆 agent, 加速完工, 巢狀 agent, 多 agent
metadata:
  pattern: pipeline
---

# Skill: parallel-dispatch (Pipeline)

> The goal is fast and accurate completion while keeping the main session's context small, so the main session holds the big picture and does not drift. There is no cap on the number of agents, and an agent may open further agents. Three things must hold: **no collisions, no drifting off task, no habitual full-suite runs**.
> This skill is a method, not more gates. Claude Code's built-in dispatch principles still apply. This only adds what they leave out: model choice, work-order format, collection, the limit on verification, and speeding up mid-run.

## 1. Whether to split (decide yourself; do not ask the user first, and do not ask about usage first)

- **Split** when there are at least two pieces that do not write the same files, and each piece reads many files or takes several minutes.
- **Dependent pieces can still start together.** If B needs A's output, fix the interface (function signatures, file formats, config key names) in both work orders and open them at the same time.
- **Do not split** when:
  - The work is smaller than the above. Each agent costs about 40k tokens of fixed startup, so small work is faster done yourself.
  - It is one dependent chain (B needs A's result to know what to do, and the interface cannot be written down). Do it in the main session.
  - The user asked to go step by step and see each result.
- For retrieval-style requests ("look this up", "research this"), follow the atom `workflow-research-fanout`.

## 2. Which model each piece gets (always fill the Agent tool's `model`; never rely on the default)

| This piece of work | model | effort |
|---|---|---|
| Judgment where a wrong call changes the design direction, or defect-hunting review; use only when `opus` at high effort is not enough (not unless truly necessary; say in the work order whether it may open more agents) | `fable` | `high` (not `max`) |
| Architecture changes, hard debugging, main implementation across many files (state the done condition and the stop point clearly: it tends to do extra) | `opus` | `high` |
| Everyday coding (small features, bug fixes, renames across a few files), data gathering and research, document editing | `sonnet` | `medium` |
| Read-only file finding, counting, listing, lint, writing commit messages (results can be checked mechanically, no judgment) | `haiku` | `low` |

- The user chooses the main session's model. Avoid Fable sub-agents unless truly necessary; if used, only for the first row, and an Opus main session may still dispatch it.
- When unsure which row, go one row up: `haiku` unsure becomes `sonnet`, `sonnet` unsure becomes `opus`. Do not split research into a swarm of parallel `haiku` agents: in testing it lost to a single `sonnet` and cost more.
- **Second opinion** (counterexamples, reviewing someone else's output): dispatch one more model that took no part in the work. If it is a model from another vendor, decisions still rest on an Anthropic model (at least `opus`): other models drift more and may claim work or problems that do not exist. A serious problem it raises is accepted only after the main session reproduces it. Anything that cannot be traced to what the user asked for is not acted on; suggestions to add more verification or more permission gates are the ones that sprawl fastest. One review round, then stop.
- The `effort` column is an explicit requirement of this skill; fill it as shown.

## 3. Before dispatch (main session; finish before sending anything out)

1. **List what each piece will write**: files, plus shared resources (git staging area and commits, service ports and running programs, memory index, config files). If two pieces write the same one, merge them into one piece or put them in order. Give each shared file exactly one writer. Only the main session commits.
2. **If there are no write conflicts, work in place; do not make copies.** Open a copy (worktree or clone) in only three cases: several pieces must change the same set of files at once, a rehearsal must not touch the live setup, or an isolated test needs a fake home directory or fake data root.
3. **Keep scratch files separate**: the main session and all sub-agents share one scratchpad, and same-named files overwrite each other. Give each piece its own subfolder under the scratchpad.
4. **Memory writes one at a time**: several concurrent `atom_write` calls overwrite each other's index. Put pieces that write cards in order, or hand the writing back to the main session.
5. **Heavy, machine-eating work runs about 6 pieces at once** (full copies with wide tests, deep scans of a whole tree, runs that exercise hooks). Queue the rest. This is a machine-resource limit, not a quota.

## 4. The work order (one per piece)

```
Goal (the user's own words): __
Big picture: __ (what the whole job is, one or two sentences)
Your piece: you handle __; put output in __; you may write: __; do not touch: __ (other pieces' files, settled decisions)
Done when: __; verify only what you changed, do not run the whole suite; do not re-run what already passed just to confirm
First output a three-sentence readback (big picture / my piece / what I won't touch), then immediately keep calling tools; do not stop partway.
Put the report in your final reply (no separate file):
0. The three-sentence readback, verbatim
1. What you did (plain words, three sentences max)
2. What you verified / did not verify (with the command and the last line of its output)
3. Found but not yours to handle (only what would make this job go wrong)
4. Where you are unsure (at most 3; write "none" if none), with why
```

- After the readback sentence, add "then immediately keep calling tools". If you only write "your first output must be the readback", the agent sends the readback and ends.
- **An agent that will split further**: pass down the same work order, with the goal wording, file ownership, and report format unchanged. When it hands back, it must mark what it verified itself and what was only reported by the next layer.

## 5. Verification: two rules only

- **Verify only what changed; do not habitually run the full suite.** This holds for agents and the main session alike. Whole-suite runs wait until the user asks for them.
- **Do not drift.** A problem that surely exists, is quick to fix, and sits in your own writable scope: fix it on the way and verify only that fix. Everything else goes into the report, and the main session decides after collecting.

Brake: if the same failure is fixed twice and still fails, stop on that item, put it in the report, and carry on with the rest.

## 6. Collecting (main session)

- **Count**: how many pieces went out, how many came back. A piece that did not come back is not written up as a conclusion; mark it "not returned", or use SendMessage to ask it to hand in.
- **An agent says PASS**: check the command and the last output line in its report. If they match, accept. If missing and the check runs in under two minutes, re-run the same command on the same scope once.
  - Mechanical work: spot-check 2 to 3 places; do not read the whole change.
  - Work where a wrong call changes the design direction: one second-opinion round as in section 2.
- **An agent says "can't be done / not supported"**: that is its inference. Read the code or run it once to confirm before passing it on.
- **Before relaying to the user**: leave out whatever is unrelated to their question; write what you verified yourself and what was only an agent's claim separately.

## 7. Speeding up mid-run (the user says "split it more", "can you go faster", "too slow")

The user means: it has run a long time, and you should judge on your own how to split off the unfinished work. Everything else stays the same: no collisions, no drifting, no habitual full-suite runs, models per the table, heavy work about 6 at once.

1. First use SendMessage to ask the running agents what is done, what is left, and which files they are editing now. The message arrives at their next tool call.
2. Split the part they have not touched into new pieces and dispatch them, and tell the running agent its scope has shrunk and what went to others. Files it is editing now are not handed out.
3. Also open any work in the main plan that does not conflict with these pieces and need not wait.
4. A piece with little output so far, where re-splitting the whole thing is faster: stop it (TaskStop) and re-dispatch under the new split.
5. Tell the user in one sentence how it was split. If it truly cannot be split (one dependent chain, or everything left edits the same files), say which of the two it is.

## 8. Claude Code limits (work with them, do not work around them)

- A sub-agent can open its own sub-agents (tested). The main session receives only the middle layer's relay and never sees the lower layer's raw text.
- The built-in Explore and Plan agents use the main session's model (not Haiku). A `model` filled on a single call overrides other settings, so state it explicitly when you want cheap.
- An agent that comes back with only a readback and zero tool calls: use SendMessage to tell it to continue; do not re-dispatch.
