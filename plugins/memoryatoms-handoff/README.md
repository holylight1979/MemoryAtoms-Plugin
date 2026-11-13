# memoryatoms-handoff

Handoff on request: warns when context is nearly full, points to /handoff when you ask for a handoff, and provides /handoff.

An extension of the MemoryAtoms plugin: installing it also enables `memoryatoms`, which it needs.

Install: `claude plugin install memoryatoms-handoff@memoryatoms`

## What it does

Adds the `/handoff` skill for writing a handoff note by hand when you want the next session to pick up where this one stopped. When the context window is nearly full, it adds one line to the wrap-up message suggesting `/handoff`; when you ask for a handoff in your own words, it points to `/handoff`. Nothing is written for the next session unless you run `/handoff`.

## What it reads and writes

The hooks only read the session state and the transcript's token usage; they write no files. `/handoff` writes the note you ask it to write. It sends nothing over the network.
