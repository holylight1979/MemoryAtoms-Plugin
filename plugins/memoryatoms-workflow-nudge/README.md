# memoryatoms-workflow-nudge

Working-style nudges: suggests fan-out for research-type requests and parallel sub-agents for independent work.

An extension of the MemoryAtoms plugin: installing it also enables `memoryatoms`, which it needs.

Install: `claude plugin install memoryatoms-workflow-nudge@memoryatoms`

## What it does

It looks at each prompt you send and, only on strong signals, adds one short suggestion for Claude: fan out the search when the request is research-shaped, or use parallel sub-agents when the request names several independent tasks. Ordinary prompts get no suggestion.

## Settings

The threshold and the per-session cap are in the `parallel_agents` section of the MemoryAtoms `workflow/config.json`. Raise `min_score` to make suggestions rarer.

## What it reads and writes

It reads the prompt text and its own counters in the session state. It sends nothing over the network and runs no external programs.
