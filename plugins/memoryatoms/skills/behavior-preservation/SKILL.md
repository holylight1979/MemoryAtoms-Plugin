---
name: behavior-preservation
description: Equivalence proof for behavior-preserving refactors: expected values come from the old source or old artifact, actual values from the new artifact, and the two sources must differ; the order / side_effect / boundary mutations must each be proven. Use when asked to rewrite code while keeping results identical, to show behavior is unchanged after a refactor, or for an equivalence proof. Triggers: 行為保持, 等值證明, 重構後結果一樣, 結果完全相同, 行為不變.
user-invocable: true
when_to_use: Triggers: 行為保持, 等值證明, 重構後結果一樣, 結果完全相同, 行為不變, behavior preservation, preserve-check, switch 改查表, 特徵測試
metadata:
  pattern: reviewer
---

# Skill: behavior-preservation (Reviewer)

> "Rewrite it but keep results identical" must be proven, not described. This skill defines only the principles and the interface; how to parse old code in a given language and how to run the new artifact in a given framework is wired in by the project (both the expected-value source and the executor are replaceable).

## Principles (three; missing any one is not a proof)

1. **Expected values come from the old source or old artifact**: parse case→value from the old code (svn BASE, an older git version) - Python via ast, other languages via project tools (roslyn, regex) into a `.json` table. Never copy by hand or infer from the new code.
2. **Actual values come from the new artifact**: actually run the new code (import, reflection, external command) and enumerate every case, including values not listed. Never guess by reading the new code.
3. **The two sources must differ**: `--expected-from` and `--actual-from` pointing at the same path is rejected outright (exit 2). Comparing a thing to itself is always equal and proves nothing.

## Three mutation kinds (run each once; behavior is preserved only if all three PASS)

| `--mutation` | What it proves | Typical miss |
|---|---|---|
| `order` | A function returning a sequence yields items in the same order | After switching the lookup to dict / set, iteration order changed |
| `side_effect` | The side effects each case triggers (log / send / event) have identical arguments and the guard sits on the same case | When extracting a pure function, the log was moved outside the table so every case now emits it |
| `boundary` | Values the old code did not list fall to the same default | The `else` value is wrong in the new table's `.get(k, default)` |

All three runs also compare every listed case→value, so a constant broken anywhere fails any of them.

## How to run

```
python scripts/preserve-check.py --expected-from <舊源碼或 .json> --actual-from <新產物 .py> \
    --mutation <order|side_effect|boundary> --func <查表函式> [--order-func <序列函式>] [--effect log]
```

- Last-line sentinel `PRESERVE_CHECK PASS|FAIL`; exit 0 = identical, 1 = differences, 2 = rejected. Paste all three outputs into the closing report; without them it does not count as verified.
- Swapping the expected source: for Python old code the ast path accepts only "one top-level if/elif chain, conditions of the form `name == constant`, branches containing only side-effect calls and `return constant`, with an else or a post-chain `return constant` as the default"; any nested if, `in`, `and/or`, or loop is rejected with exit 2 and the line number printed - it never skips part and still claims full enumeration; docstrings and comments do not affect the verdict. Non-Python old code (C#, Lua…) is not guessed by this script: first parse it with a project tool (roslyn, regex) into a `.json` (format in the script header; zero cases is also rejected), then feed that in.
- Swapping the executor: `--actual-cmd "<command> {key}"` uses an external program as executor (e.g. a small dotnet program reflecting into a dll); it cannot see side effects, so `side_effect` says it is unsupported and FAILs - that kind needs its own executor.
- State plainly what cannot be proven: timing, threads, and live state are outside this method.
- `.json` expected-table format (produced by a project tool for non-Python old code): `{"values": {key: value}, "default": value, "effects": {key: [args]}, "order": [...]}`; keys are always strings and are aligned with `str()` when comparing.

## What to hand in at the end (Stop Equivalence Gate check)

When the intent is a refactor, program files were changed this turn, and you declare completion, the closing check covers two things (rules `REFACTOR_NEEDS_EQUIVALENCE`, `PERF_HOT_PATH_NEEDS_MEASURE`, in `hooks/wg_release_rules.py`):

- Equivalence: after the last program-file edit, run this script three times (`--mutation order` / `side_effect` / `boundary`, one each, every last line `PRESERVE_CHECK PASS`); or write one line in the closing text, `等值證法｜不適用｜<理由>` (format "equivalence proof | not applicable | <reason>"; e.g. what was refactored is not a lookup/dispatch, there is no old artifact to enumerate, or only tests changed). The reason is logged to `Logs/guard-equivalence.jsonl`. If missing, the gate blocks; once the block budget is used up it asks the user. Whether it is "a refactor" is judged only from what the user themselves typed.
- Hot path: answer in the closing text `熱路徑｜是｜<哪裡>` ("hot path | yes | <where>") plus a measurement receipt (a self-built measurement whose last line is `<NAME> DONE k=v`) or `量測｜不做｜<理由>` ("measure | skip | <reason>"); or `熱路徑｜否｜<為什麼>` ("hot path | no | <why>"). You decide whether it is a hot path and how to measure; the program only checks that you answered and the receipt exists.

## What this skill does not do

- It does not write parsers or executors for the project; it only takes two sources and the results of the three mutation kinds.
- It does not accept "it compiles" or "looks the same" as evidence.
