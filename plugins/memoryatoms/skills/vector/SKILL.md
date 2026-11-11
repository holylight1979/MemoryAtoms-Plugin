---
name: vector
description: Vector service management - start/stop/rebuild the Vector Service (LanceDB + Ollama). 向量服務管理：啟動/停止/重建 Vector Service
---

# /memoryatoms:vector — Vector service management

> One entry for the memory vector service: status, start/stop, index rebuild, semantic search.
> Global skill; works in any project.

---

## Usage

```
/memoryatoms:vector                    # default: show status
/memoryatoms:vector status             # service status + index stats
/memoryatoms:vector start              # start the daemon
/memoryatoms:vector stop               # stop the daemon
/memoryatoms:vector reindex            # full index rebuild
/memoryatoms:vector reindex --incremental  # incremental index
/memoryatoms:vector search keywords    # semantic search (for debugging)
```

---

## Argument parsing

Take the subcommand from `$ARGUMENTS`:
- empty / `status` → status query
- `start` → start
- `stop` → stop
- `reindex [--incremental]` → index rebuild
- `search <query>` → semantic search

---

## Subcommand: status (default)

Run with the Bash tool:

```bash
python "${CLAUDE_PLUGIN_ROOT}/tools/rag-engine.py" status
```

Interpret the output and report:
- Whether the service is running (the port follows the data root: `python "${CLAUDE_PLUGIN_ROOT}/tools/memory-vector-service/starter.py" --print-port`)
- Indexed file count / total vector count
- Ollama embedding model status
- Last index time

## Subcommand: start

```bash
python "${CLAUDE_PLUGIN_ROOT}/tools/rag-engine.py" start
```

After starting, run `health` automatically to confirm success.

## Subcommand: stop

```bash
python "${CLAUDE_PLUGIN_ROOT}/tools/rag-engine.py" stop
```

## Subcommand: reindex

```bash
python "${CLAUDE_PLUGIN_ROOT}/tools/rag-engine.py" index [--incremental]
```

- No flag → full rebuild (delete the old index and re-embed every atom)
- `--incremental` → process only added/modified files

Before running, warn that a full rebuild takes a while (depends on the atom count) and confirm the user wants to continue.
Incremental indexing runs directly with no confirmation.

When done, show the index stats.

## Subcommand: search

```bash
python "${CLAUDE_PLUGIN_ROOT}/tools/rag-engine.py" search "query text" --top-k 5
```

Show the results:
- For each result: the atom name, similarity score, and matching snippet
- Purpose: verify vector search quality and debug retrieval problems
