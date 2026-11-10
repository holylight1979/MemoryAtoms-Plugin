# memoryatoms-dashboard

Local web dashboard served from the MCP process: dashboard, brain-world, and the wrap-up report HUD.

An extension of the MemoryAtoms plugin: installing it also enables `memoryatoms`, which it needs.

Install: `claude plugin install memoryatoms-dashboard@memoryatoms`

## What it reads and sends

Serves a local web page from the MCP process on 127.0.0.1 (port 3848 by default, WG_DASHBOARD_PORT or dashboard_port in workflow/config.json to change it).
