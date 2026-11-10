# memoryatoms-statusline

Claude Code status line: edited files, knowledge queue, vector service, wrap-up report (zero tokens)

An extension of the MemoryAtoms plugin: installing it also enables `memoryatoms`, which it needs.

Install: `claude plugin install memoryatoms-statusline@memoryatoms`

## Turning the status line on

Claude Code plugins cannot set your status line (plugin settings only take `agent` and `subagentStatusLine`), so this plugin has no
hooks and no MCP server: it only brings the status-line script and leaves turning it on to you. It shows edited files, the knowledge
queue, the vector service and the wrap-up report, and costs no tokens.

Add this to the `statusLine` key of your user settings (`~/.claude/settings.json`, or `$CLAUDE_CONFIG_DIR/settings.json`). It looks both
plugins up in Claude Code's own list of installed plugins every time, so it keeps working after an update moves them to new version
folders:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python -c \"import json,os,runpy;c=os.environ.get('CLAUDE_CONFIG_DIR') or os.path.expanduser('~/.claude');d=json.load(open(os.path.join(c,'plugins','installed_plugins.json'),encoding='utf-8'))['plugins'];os.environ['WG_CORE_ROOT']=d['memoryatoms@memoryatoms'][0]['installPath'];runpy.run_path(os.path.join(d['memoryatoms-statusline@memoryatoms'][0]['installPath'],'tools','statusline.py'),run_name='__main__')\"",
    "padding": 0,
    "refreshInterval": 10
  }
}
```

- Use `python3` instead of `python` if that is how Python 3.10+ is named on your machine.
- If you installed from another marketplace name, replace `@memoryatoms` in both ids with the one shown in
  `~/.claude/plugins/installed_plugins.json`.
- The script needs the core plugin `memoryatoms` (it reads the core's libraries through `WG_CORE_ROOT`, which the command above sets).
- To turn it off, remove the `statusLine` key.

---

## 開啟狀態列

Claude Code 的插件不能設定狀態列（插件的 settings 只認 `agent` 與 `subagentStatusLine`），所以這個插件沒有 hook、沒有 MCP 伺服器，
只帶狀態列腳本，要不要開由你決定。它顯示改過的檔、知識佇列、向量服務、收尾報告，不花 token。

把上方英文段那段 JSON 加進使用者設定（`~/.claude/settings.json`，或 `$CLAUDE_CONFIG_DIR/settings.json`）的 `statusLine`。它每次都從
Claude Code 自己的已安裝插件清單找兩個插件的位置，所以升級換了版本資料夾也照常能用。

- 你的機器上 Python 3.10+ 叫 `python3` 的話，把 `python` 換成 `python3`。
- 從別的市集名安裝的，把兩個 id 裡的 `@memoryatoms` 換成 `~/.claude/plugins/installed_plugins.json` 裡看到的那個。
- 腳本要靠核心插件 `memoryatoms`（透過上面指令設的 `WG_CORE_ROOT` 讀核心的程式庫）。
- 要關掉，刪掉 `statusLine` 這個鍵。
