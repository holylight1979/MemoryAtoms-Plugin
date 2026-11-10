# memoryatoms-codex

Codex integration: /codex second opinion and build delegation, plus Codex Companion background review and acceptance judge.

An extension of the MemoryAtoms plugin: installing it also enables `memoryatoms`, which it needs.

Install: `claude plugin install memoryatoms-codex@memoryatoms`

## What it reads and sends

Calls the codex CLI you have installed (or headless claude -p as a fallback judge) from background processes; nothing is sent unless that CLI is installed and signed in.
