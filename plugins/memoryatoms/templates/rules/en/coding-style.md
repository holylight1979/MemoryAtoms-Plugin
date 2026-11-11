---
paths:
  - "**/*.{py,pyw,js,mjs,cjs,ts,tsx,jsx,cs,c,cc,cpp,h,hpp,java,kt,go,rs,rb,php,lua,swift,m,mm,sh,ps1,psm1,bat,cmd,sql,shader,hlsl,cginc}"
---
# Coding leanings

> Priority: correctness and safety boundaries > the user's goal this time > real constraints > the project's existing practice > readable, verifiable simplicity > smallest change surface > personal syntax taste. When the goal conflicts with rule files, records, or existing code, follow "Handling conflicting settings" in `rules/code-thinking.md`. Minimalism always ranks after correctness.

## The yardstick for design trade-offs (use it both to judge good design and to critique the current state)
- Classification and handling must be clear:
  - Data: data itself can be laid out as a two-dimensional table (rows×columns), so the application should be intuitive and readable at a glance (e.g. SQL table columns); this direction beats the convenience of any package, format, or framework.
  - Values and data: avoid sprawl. If there are only 5 values, write an enum of 5 values; opening it up as a byte (0～255) so the reader must guess which values are legal is the counterexample.
  - Readability: reduce anonymity (functions, types, magic numbers without names); comments in plain language.
- Less binding: data need not be bound to a serialization format or to masses of generated objects; tool-generated code binds you to a package and loses readability and room for abstract thought — that does not count as an advantage.
- Concise, readable, easy to picture: let the reader directly see the shape and flow of the data.
- Flexible access: fetch what you need, write queries to the need; tool-generated code is only an extension that follows once things are readable.

## Structure
- First pick the data model and standard operations closest to the problem's meaning, then the control structure.
- Do not rank if / for / while as substitutes for each other; branches handle choice, loops handle repetition.
- When several constructs express the same meaning, take the one that leaves the reader the least state to remember: straight line > guard clause / early return > bounded iteration (for-each, map/filter) > single-level branch > unbounded loop > recursion > high-level abstraction / metaprogramming.
- Use recursion only for naturally recursive problems with bounded depth; use state machines only when state transition is the problem itself.
- Flattening step one: kick out exceptions first so the main path is not indented; inside loops use continue / break to leave early.
- Flattening step two: name complex conditions as predicates; when an if chain grows long, use a lookup table or dict dispatch.
- Flattening step three: when nesting comes from repeated searching, prebuild a Set / Dict index, which often also turns repeated scans into a single lookup.
- Nesting ≤2 needs no discussion.
- Nesting =3 is the review line: if the three levels each guard an independent condition, the reader must hold three things at once — flatten it; if it is a single local decision that can still be followed in a straight line, it may stay.
- Nesting ≥4 needs refactoring.
- The three lines above only govern the sections I write or change this time; existing nesting need not be refactored along the way.
- Do not over-flatten: extract a function only with ≥2 call sites, or when it can be named with one noun and has real content; do not create one-off tiny functions just to reduce nesting.
- A comprehension may have only one for and one if; clear > clever.
- One file shows one thing end to end; extracting across files must buy clear reuse, isolation, or a responsibility boundary.

## Minimalism
- Add no parameters, settings, abstractions, or defensive branches that were not asked for; external boundaries and known failure modes are still handled.
- Minimalism never cuts observability: error signals, logs, resource release, and security checks are not deleted; silent failure is forbidden; a record field that tells two different causes apart does not count as an extra parameter.
- Extract shared code only the 2nd time "knowledge that must change together" appears; similar-looking text does not count as duplication.
- Dead code, temp bits, and noise comments you created yourself are deleted on the spot; do not get attached to code.

## Rhythm
- Read code with a purpose: for each implementation goal, first list the contracts, call sites, data flows, and tests to confirm.
- Expand reading hop by hop along real dependencies from the search hit; do not load a whole package just because it is the same module.
- Stop condition: stop once you can state inputs and outputs, invariants, affected paths, and how to verify; read one more hop only for unknowns that would change a decision.
- Old code is only evidence; do not assume it is correct.
- Side dead code and problems are not touched mid-change; note them first, and handle them per the anti-evasion contract after this section is done.
- Start work with the smallest verifiable hypothesis, advance in small steps; after each section, tidy only the scope touched this time.
- A script family has one entry point with --help; every script starts with a header paragraph "what it does, how to run it"; --help is the authority for full parameters.

## Self-check
> Three stopping points: before acting, when results contradict expectations, before delivering. The bright point = the place where you stop and observe calmly.
- Each time confirm only four things: the current goal, known facts, working hypotheses, the next distinguishing action.
- Evidence contradicts expectation: do not defend the original plan or widen the change; update the hypothesis first, then do the smallest check that can tell candidate causes apart.
- Before delivering, actually run against the changed scope; the report separates verified facts, inferences, and unknowns, and does not fill evidence gaps with a confident tone.
- Reread along the data flow from a stranger-maintainer's view; sections that cannot be explained continuously get tidied, but that does not automatically mean splitting and rewriting.
- A verification script ends with one sentinel line `<NAME>_CHECK PASS` or `<NAME>_CHECK FAIL` (format `^[A-Z][A-Z0-9_]*_CHECK (PASS|FAIL)$`), with exit 0／1 matching; printing only `<NAME>_CHECK` without the suffix does not count as PASS.
- A measurement script ends with `<NAME> DONE k=v k=v` (format `^[A-Z][A-Z0-9_]* DONE( \w+=\S+)*$`); the numbers are a yardstick, not a verdict, and it does not print PASS.
- When an existing tool's last line has a different format, do not change the tool; add a `sentinel_regex` to that item in `prebuild_checks[]` of the project's `.claude/overview-hub.json` to map it to one of the two formats above.
- Verification runs only the changed scope and truly related cases; the full suite runs only when the user names it. If a verification program really must run beyond the single tool-call limit (10 minutes), after printing its sentinel it writes its own receipt (call `write_self_receipt` from the memory system's `lib/self_receipt.py`, writing to `workflow/verify-ledger/_runs/`; never hand-write one) so the yardstick gate can see a result finished in the background; run a yardstick alone, using the exact string from the parts table.
