---
name: setup-vector
description: Optional vector search setup in four steps (Python packages → Ollama → embedding model → extraction model; the two models are about 6.1 GB, plus Ollama itself); every step first says what it downloads, how big it is and where it goes, and runs only after the user agrees. Use when the session start shows "vector search is not installed" with /memoryatoms:setup-vector, or the user asks to set up vector or semantic search. 觸發詞：裝向量, 安裝向量搜尋, 向量搜尋沒裝, setup-vector, 語意搜尋
user-invocable: true
when_to_use: Triggers - setup-vector, set up vector search, install vector search, vector search is not installed, semantic search setup. 觸發詞：裝向量, 安裝向量搜尋, 向量搜尋沒裝, 設定向量, 語意搜尋, setup-vector
---

# /memoryatoms:setup-vector — optional vector search

> Without vector search the memory still works on trigger words and BM25 keyword matching; vector search adds
> meaning-based recall (reworded questions reach the right card 71.4% of the time instead of 51.8%).
> Nothing here runs on its own: every step is shown first and runs only after the user says yes to that step.

## Hard rules

- **Plan first, every step.** Before each step, run the plan for that step and show the user its line as is:
  what is downloaded, how big, where it goes. Then ask one question ("Run step N?") and wait.
- **One step per yes.** A yes covers only the step it was asked for. Never chain `--apply` calls.
- Never install packages into the system Python, never pipe a download into a shell, never open a browser.
- If the user says no or "not now": stop. If they never want it, tell them they can turn the session-start line off:
  set `vector_search.invite` to `false` in `workflow/config.json` of their data folder.

## Steps

Always pass the plugin's data folder (the Bash tool does not see `CLAUDE_PLUGIN_DATA`, so it goes on the command line):

1. **Show the whole plan** (nothing is downloaded or run):

   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/tools/setup-vector.py" --plugin-data "${CLAUDE_PLUGIN_DATA}"
   ```

   Relay the four lines (done / to do) and say the total: the two models are about 6.1 GB (embedding 4.7 GB, extraction 1.4 GB), plus Ollama itself (see its line).

2. **Step 1 — Python packages** (about 100–130 MB; into `${CLAUDE_PLUGIN_DATA}/venv`, pinned versions from
   `tools/memory-vector-service/requirements.lock`). After the user agrees:

   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/tools/setup-vector.py" --plugin-data "${CLAUDE_PLUGIN_DATA}" --step venv --apply
   ```

3. **Step 2 — Ollama** (the program that computes embeddings). Show the plan line first:

   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/tools/setup-vector.py" --plugin-data "${CLAUDE_PLUGIN_DATA}" --step ollama
   ```

   If Ollama is already installed the step is skipped. Otherwise offer two ways and let the user pick:
   install it themselves from https://ollama.com/download (then re-run the plan), or let this step download the
   official installer from Ollama's GitHub release, check its published sha256 (and on Windows its signature),
   and only then run it. On Linux the official script asks for sudo; tell the user before running it.
   After the user agrees:

   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/tools/setup-vector.py" --plugin-data "${CLAUDE_PLUGIN_DATA}" --step ollama --apply
   ```

4. **Step 3 — embedding model** (`ollama pull qwen3-embedding`, about 4.7 GB, stored by Ollama). Show the plan
   line, then after the user agrees:

   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/tools/setup-vector.py" --plugin-data "${CLAUDE_PLUGIN_DATA}" --step model --apply
   ```

5. **Step 4 — extraction model** (`ollama pull qwen3:1.7b`, about 1.4 GB, stored by Ollama). End-of-session
   harvesting uses this small local model to pull knowledge out of the session. Without it harvesting does not
   run and says nothing on screen (each try only writes one ERROR line to `Logs/atom-debug.log` in the data
   folder). The user can skip it if they have set a remote LLM. Show the plan line, then after the user agrees:

   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/tools/setup-vector.py" --plugin-data "${CLAUDE_PLUGIN_DATA}" --step extract-model --apply
   ```

6. When steps 1-3 are done the vector service starts on its own at the next session start (the session-start
   hook finds the venv); tell the user to start a new session.

## Reading the output

- The last line is `SETUP_VECTOR PLAN venv=… ollama=… model=… extract-model=…` (plan) or `SETUP_VECTOR DONE|FAIL step=…` (a step).
- On FAIL, relay the line above it as is (it says what failed and what to do) and stop; do not retry on your own.
- `fake=1` means `WG_FAKE_EXEC=1` was set: nothing was really downloaded or run (tests only).
