---
name: handoff
description: Cross-session handoff prompt builder - enforces a self-sufficient 6-block template so the next session does not start blind. 跨 Session Handoff：給下 session 的 prompt、續接 prompt、交接、寫 next-phase。
---

# /memoryatoms-handoff:handoff — cross-session handoff prompt builder

> Enforces a self-sufficient 6-block template so the next session does not start blind.
> Global skill. Pairs with `/continue` (the reading side, shipped with the core): this skill is the writing side.

---

## Trigger

Run proactively when the user asks for "a prompt for the next session", "a continuation prompt", "handoff", or "write next-phase" (給下 session 的 prompt、續接 prompt、交接、寫 next-phase).
If the user already wrote one by hand, check it against the 6-block list and fill the gaps.

---

## Core principle

**The reader is not the current conversation partner.** The next Claude saw nothing of this conversation.
Pronouns like "what we just discussed", "what was said before", "that method" cannot be resolved by the next Claude.

---

## Required 6 blocks (missing one = refuse to finish)

### 1.【前置脈絡】(Context)
- Absolute project root path (e.g. `D:/work/acme-shop/.claude/`)
- Working branch / working directory
- Why this is being done (**include the why**, not only the what)
- **Copy the original goal verbatim from its source** (the user's own words in the conversation), not from the previous handoff: retelling a retelling shrinks the goal round by round down to the task at hand.
- **The goal's full arc (all four parts, even for small tasks)**: original goal (user's words) -> phase goals (split into which phases, status of each) -> brief history (one or two sentences per phase, with commits) -> result against the original goal (how far, what remains, judged by what numbers). Writing only the method or only a pointer to a document leaves the next session knowing "go do X" but not what to judge it against.

### 2.【已完成】(Done)
- Phase number or name
- Commit hash (first 8 chars) + push status
- Verification passed (tests / build / manual)

### 3.【權威來源】(Authoritative sources)
- List of file path:line
- What the next Claude should **read first** to get oriented
- External resources (document URLs, intranet paths, permission requirements)

### 4.【產出位置】(Output locations)
- Files already produced (paths)
- Files still to be produced (path + format)

### 5.【做法】(Method)
- Step list (bullets are fine)
- **State the tool choices** (so the next Claude does not re-evaluate)
  - e.g. use curl, not a browser; use the atom_write MCP, not the Write tool

### 6.【決策依據】(Decision basis)
- Why this method
- Which alternatives were rejected and why
- Known limits / known pitfalls
- **How to think** (a to-do-only handoff leaves the new session with just a task list): the yardsticks used, what the user corrected this session (with the original sentence), where it went astray with evidence and how it was pulled back; mark each principle's source - said by the user (original sentence) or inferred by the AI.

---

## Output format

Wrap the whole prompt in one ` ``` ` code block so the user can copy and paste it.
Always also save the same content to the project's `{project}/.claude/memory/_staging/next-phase-{name}.md` (create `.claude/memory/_staging/` if missing); the next `/continue` reads it automatically.
If the write is blocked (gate deny, permission refused), do not redirect it to a scratchpad or other temp location: print the block message verbatim and the path you meant to write to, and still give the code block.

---

## Anti-patterns (self-check list)

Check before running; if any hits, refuse to finish and fill the gap:

- "Continue X Phase 2" as a one-line prompt
- Only the what, no why (the next Claude has no judgment standard)
- No goal arc: only a method, a measuring method, or one document path, with no original goal, phases, history, or result against the goal (e.g. "tomorrow's blocked-count" with only how to count and a baseline number)
- Pronouns that point at the current conversation: "we", "just now", "before", "that"
- Citing "a decision in the conversation" without listing the decision itself
- Missing authoritative sources (the next Claude does not know what to read first)
- Missing commit hash (the next Claude cannot locate the code version of "done")

---

## Relation to /continue

| Skill | Role | Action |
|-------|------|--------|
| `/memoryatoms-handoff:handoff` | **Writing side** | Produces the self-sufficient prompt, optionally saves it to staging |
| `/continue` (core) | **Reading side** | Reads `_staging/next-phase-*.md` and executes |

Not saving to staging is fine too: just give the user a code block to copy.

---

## Example contrast

### Bad example (a one-line handoff)
```
繼續 ShopApi 知識庫建置 Phase 2：ShopAdmin 管理頁交叉對應。
```
-> The next Claude does not know: where the project is, what Phase 1 did, where the authoritative sources are, how to get into ShopAdmin.

### Good example (self-sufficient version)
```
【前置脈絡】
- 專案：D:/work/acme-shop/.claude/（acme-shop 伺服器，team memory repo）
- 任務：ShopApi 知識庫建置 Phase 2 — ShopAdmin 管理頁交叉對應
- Why：team 同事 alice 之前在 a1b2c3d 補齊了 40 支訂單 API 的參數，需與管理頁對齊

【已完成】
- Phase 1：commit e4f5a6b 已 push origin/master
- 權威來源：_AIDocs/API_Endpoints_Report.md（40 支 API 完整參數）
- Phase 1 產出：D:/work/acme-shop/.claude/memory/api-directory.md（40 支 API §1-4 + §5 ShopAdmin 占位）

【權威來源】
- API 清單：ShopServer 40 支 API（api-directory.md §1-4）
- 同事補齊：commit a1b2c3d

【產出位置】
- 既有：api-directory.md §5 ShopAdmin 占位
- 接下來：shopadmin-page-map.md（新檔）→ 回寫 §5

【做法】
1. 從本機直接連測試站的管理頁（不開瀏覽器，省 Playwright 開銷）
2. 用 curl 逐頁抓 HTML，比對 ShopServer 40 支 API
3. curl 抓不下來的頁才改用瀏覽器補
4. 產出 shopadmin-page-map.md → 回寫 §5 → commit + push

【決策依據】
- 為什麼用 curl：本機背景連測試站夠用，不需開視窗（之前討論結論）
- 為什麼不全用 Playwright：開銷大且非必要；只在 curl 失敗時 fallback
- 已知坑：ShopAdmin 測試帳密在 D:/work/acme-shop/.claude/secrets/（git ignore），讀檔取
```

---

## Step 1: Judge the trigger

From the user's recent messages, catch one of these intents:
- "a prompt for the next session", "the prompt for the next session"
- "continue", "handoff", "pick up next time"
- "write next-phase"

If none is caught, do not run; reply "No handoff intent detected, please state it explicitly."

## Step 2: Collect the 6 blocks

Fill the 6 blocks one by one from the current conversation. If any block lacks information, ask the user; **do not guess**.

## Step 3: Check against the anti-pattern list

Go item by item; if any hits, go back and fill it; **do not deliver**.

## Step 3.5: Adversarial self-review gate (mandatory before delivery; no skipping)

> A complete structure (all 6 blocks present) is not correct meaning. **The author does not automatically see their own gaps**: in practice CC found the gaps in its own next-phase.md (ones that cause distortion or wrong understanding) only after the user said "do two careful confirmations". So this gate forces one critical review round; it cannot be skipped or run only in the head.

Ask each item; any "no" means go back and fix, do not deliver:

- [ ] After reading, does the new session know **why** (outcome / why), not only what?
- [ ] Does the prompt itself carry **the goal's full arc** (original goal, phase goals, brief history, result against the goal), without needing "go read some document" to recover it?
- [ ] Does every locked decision carry **a one-line reason**? Do rejected alternatives state why?
- [ ] Are **open questions / uncertainty** in their own section, each a concrete answerable question?
- [ ] Does it hold only the **delta** (specific to this session, changing, essential for the receiver)? General rules / SOPs are linked with `[[link]]`, not pasted whole?
- [ ] Are load-bearing numbers / paths / identifiers kept **verbatim**, not paraphrased into vague descriptions?
- [ ] Are **assumptions vs verified** marked? Unverified items marked "to confirm, do not lock in on this"?
- [ ] Are old/new contradictions **explicitly ruled** (X wins, Y void), not left side by side for the downstream? Is the same thing told two ways in different sections?
- [ ] Are key constraints / negative conditions at the **start and end**, not buried in the middle?
- [ ] **Judgment entry**: things that still need judgment (what to test, who is affected, whether something is a false alarm) are written as "left to the next session to judge, with reasons", not hard-coded as fixed rules? Only already-judged things are hard-coded.
- [ ] **One more pass before handing over**: check sentence by sentence against the user's original words, then against the plan; what this session just changed affects which already-delivered content (lists, plans)? If any, change it too.

Knowledge basis: atoms [[handoff-綜觀品質與抗失真寫法]], [[跨session資訊失真機制與對策]].

> **Handoff relies on Step 3.5 self-review**: writing to `_staging/next-phase*.md` does not automatically call Codex for review (Companion's `soft_gate.handoff_review` is false); the Step 3.5 self-review is the mandatory hard gate. To get another context to find blind spots in this handoff, call skill `codex` for a read-only review, using the first 8 questions as the checklist (the last two need to know what this session changed and judged, so only the author can self-review them).

## Step 4: Output

Wrap in a code block. If the user wants it saved to staging:
```bash
# write path
{project}/.claude/memory/_staging/next-phase-{name}.md
```

Then tell the user: "Prompt produced, paste it at the start of the next session" or "Saved to staging, the next /continue reads it automatically".
