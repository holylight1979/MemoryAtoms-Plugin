# memoryatoms-wisdom

Reflection stats: records how each session went and shows at most two self-awareness lines at session start.

An extension of the MemoryAtoms plugin: installing it also enables `memoryatoms`, which it needs.

Install: `claude plugin install memoryatoms-wisdom@memoryatoms`

## What it does

After each session it records how the work went, by task type, in a sliding window of the last ten outcomes. At the start of a later session it may show one or two short self-awareness lines, for example when a kind of task has been going wrong recently. With no history yet it shows nothing and costs no tokens.

## What it reads and writes

It reads and writes only its own statistics file under the MemoryAtoms data folder. It sends nothing over the network and runs no external programs.

## Turning it off

Disable the plugin with `/plugin`, and the lines stop appearing. The statistics file stays where it is.
