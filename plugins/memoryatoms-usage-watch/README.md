# memoryatoms-usage-watch

Claude account usage monitor: scheduled quota checks, anomaly notifications, and a start-of-session hint when the monitor is not running.

An extension of the MemoryAtoms plugin: installing it also enables `memoryatoms`, which it needs.

Install: `claude plugin install memoryatoms-usage-watch@memoryatoms`

## What it reads and sends

Reads Claude Code's login token from ~/.claude/.credentials.json and sends it to api.anthropic.com to ask for your account quota; optional notifications go to the Discord webhook you configure. For that reason it is offered in the GitHub marketplace only and is not submitted to the claude.com plugin directory.
