# memoryatoms-handoff

Automatic handoff notes: prepares next-phase-auto.md before compaction and at session end, warns when context is nearly full, and provides /handoff.

An extension of the MemoryAtoms plugin: installing it also enables `memoryatoms`, which it needs.

Install: `claude plugin install memoryatoms-handoff@memoryatoms`

## What it does

Right before Claude Code compacts a long conversation, and again at session end, it writes a handoff note (`next-phase-auto.md`) in the MemoryAtoms staging folder so the next session can pick up where this one stopped. After compaction it asks Claude once to fill in the parts only Claude can judge. It also warns when the context window is nearly full, and adds the `/handoff` skill for writing a note by hand.

## What it reads and writes

It reads the session state and the compaction summary, runs read-only `git` queries (status and recent log) to describe what changed, and writes only the handoff note. A hand-written note is never overwritten. It sends nothing over the network.
