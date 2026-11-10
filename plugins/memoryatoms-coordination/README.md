# memoryatoms-coordination

Multi-session coordination: warns when another live session edits the same file or is about to finish with git (warns only, never blocks).

An extension of the MemoryAtoms plugin: installing it also enables `memoryatoms`, which it needs.

Install: `claude plugin install memoryatoms-coordination@memoryatoms`

## What it does

When two Claude Code sessions are open on the same machine, it warns the one that is about to edit a file the other session has already changed, and warns before `git add -A` or `git reset --hard` while the other session has uncommitted work in the same folder. It only warns. It never blocks a tool call.

## What it reads and writes

It reads the session state files that MemoryAtoms keeps for live sessions, and runs `claude agents --json` to see which sessions are alive. It writes nothing of its own and sends nothing over the network.
