---
paths:
  - "**/*.{py,pyw,js,mjs,cjs,ts,tsx,jsx,cs,c,cc,cpp,h,hpp,java,kt,go,rs,rb,php,lua,swift,m,mm,sh,ps1,psm1,bat,cmd,sql,shader,hlsl,cginc}"
---
# How to think about a code request (loaded when touching code files)

> When it loads: Claude Code loads it automatically when the AI reads or changes a code file. When analyzing, designing, refactoring, or changing code, write two sentences before reading the code: the goal (copied from the user's own words) and the simplest shape if written from scratch; what follows here is the detail for after reading.

## Ask for the need first, then read the current state (overview＋side view)
- Copy the goal from the user's own words; judge the simplest from-scratch shape with coding-style's "yardstick for design trade-offs", using only nouns from the user's world (player, gold, table, column); if nouns from the existing implementation appear (existing format, framework, class), the current state has colored you.
- After reading the current state, compare item by item, and classify the extra parts per "Handling conflicting settings" below: a real constraint → write its cost; an existing practice → it is a smell, say so, do not copy it. If the need cannot be written down, that is the question to ask the user.
- Existing code only proves the current state, not how it should be written.
- Before delivering, ask again: how far did the first step move the goal, and did the new code copy the smell just pointed out.

## Handling conflicting settings
- **Classify first, then compare priority**: when you read sentences like "always, do not use, forbidden, must, high risk", first decide which kind it is:
  - Safety boundary (would break live data, irreversible, touches someone else's things): it decides what to confirm and how to protect before doing it, not whether to do it.
  - Real constraint (data already live, external protocols, another team's code): count it as a cost to pay, and write what removing it would take.
  - Existing practice ("how we do it now" written as a rule, rollbacks and rejections in records): only the current state; if it blocks the goal, it is the one to change.
  - Cannot tell: treat it as existing practice and mark it "inference".
- **Write conflicts as obstacles**: each thing blocking the goal gets one line "obstacle｜original sentence (file:line)｜which kind｜what removing it takes"; the conclusion is the path toward the goal, and removing obstacles is counted as part of the path.
- **Write the end state first in the conclusion**: first write what the ideal shape looks like once landed, then which step on the road to it this one is; for parts deferred by timing, state when and on what condition they get done.
- **When you ask, ask about the obstacle**: removing an obstacle takes time (estimated over two minutes) or is irreversible → stop first and ask in one sentence "change／remove 〈that one〉?" with a recommendation; do not ask "change the goal?".
- **Only one case allows proposing a goal change**: a real constraint makes the goal impossible (not expensive — impossible), with evidence, together with the path closest to the goal.
- Priority orders in other files (coding-style etc.) only govern how-to-write trade-offs; when they conflict with this section, this section wins.
