# MemoryAtoms

Claude Code 的原子記憶。MemoryAtoms 把你和 Claude 學到的事存成一張張小的 Markdown 卡片；每一句話都把合用的卡送到 Claude 眼前；Claude 透過 MCP 伺服器寫卡、找卡；每場 session 學到的東西收割成新卡。出廠帶 485 張日常開發用的卡。

English: [README.md](README.md)

- 作者：holylight
- 授權：GPL-3.0-only（見 `LICENSE`）

這個插件只有記憶本體：記卡、找卡、在用得上的時候送回來。

## 需求

- 支援插件的 Claude Code
- Python 3.10 以上（hook 與工具是 Python）
- Node.js 18 以上（hook 啟動器與 MCP 伺服器是 Node）
- git：只有專案記憶進版控、同步、接組織層時才用

找不到 Python 時，每場第一句會出一行 `[Guardian:Degraded]`，說查過哪些地方、怎麼修；修好前記憶不會動。

## 安裝與第一場

1. 加市集：`/plugin marketplace add <帳號>/MemoryAtoms-Plugin`
2. 安裝：`/plugin install memoryatoms@memoryatoms`。Claude Code 2.1.275 以上可把 1、2 步併成一行：`/plugin install memoryatoms --marketplace <帳號>/MemoryAtoms-Plugin`。安裝對話框問五項：
   - `data_dir`（資料夾）：卡片、索引、紀錄放哪。預設 `~/.memoryatoms`。從 git 安裝版換過來的人填 `~/.claude`。
   - `language`：提示與訊息用 `en` 或 `zh-TW`。卡片不翻譯。
   - `python`：Python 3.10+ 的路徑。留空＝自動找。
   - `remote_llm_user`、`remote_llm_password`：只有你設的遠端 LLM 後端要登入時才填（見「傳到哪」），其他情況留空。密碼輸入時遮住，存在系統的安全憑證庫，不寫進 `settings.json`。
   之後可用 `/plugin configure` 改。
3. `/reload-plugins` 或重開 Claude Code。
4. 第一場開場會建資料夾、用模板寫 `IDENTITY.md` 與 `USER.md`、展開 485 張出廠卡並建索引。作者機上第一場開場約 9～17 秒，之後每場 2 秒多。
5. Claude 第一次寫卡時，Claude Code 會問你要不要允許這個 MCP 工具。插件不能預先放行。
6. 選做：`/memoryatoms:setup-vector` 開語意搜尋（見「下載什麼」）。

每次開場也會建議你跑 `/memory` 關掉 Claude Code 內建的自動記憶。插件沒辦法替你關（它從不改你的設定）。理由：兩套都開時筆記存兩處、內建那套每場約多 500 tokens，而且它的規矩可能和寫卡規矩衝突。

## 執行什麼

### Hook（12 種事件、13 條）

每條都是 `node ${CLAUDE_PLUGIN_ROOT}/hooks/run.js <腳本>.py`。啟動器找 Python（`python` 設定 → 快取路徑 → Windows 的 `py -3`／`python`，其他系統的 `python3`／`python`）再跑腳本。沒有任何 hook 掛在 Bash 或 PowerShell 上。hook 唯一會拒絕的工具呼叫，是格式不對的卡片檔、或 Claude Code 原生記憶資料夾上的 Write／Edit；拒絕訊息會叫 Claude 改用 `atom_write` 工具。

| 事件 | 時限 | 做什麼 |
|---|---|---|
| SessionStart | 20 秒 | 建／更新卡片索引、展開或更新出廠卡、送開場卡片、壓縮後把卡片送回來、印一行式提示（例如「向量搜尋還沒裝」）。 |
| SessionStart | 10 秒 | 個人化啟動：送 `IDENTITY.md`、`USER.md`、卡片目錄與寫卡規矩；兩個檔不在就用模板建。 |
| UserPromptSubmit | 8 秒 | 找出合用這句話的卡，加進 Claude 的脈絡。 |
| PreToolUse（Write、Edit、Agent、Task） | 5 秒 | 直接改卡片檔時導回 `atom_write` 工具；子代理開工時給它合用的卡。 |
| PostToolUse（Write、Edit、Agent、Task 與 MCP 工具 atom_write、atom_retire、knowledge_harvest_report） | 5 秒 | 改過的卡重新索引、記卡片收據與來源、核對收割回報、記子代理拿到哪些卡。 |
| PreCompact | 5 秒 | 壓縮前記下脈絡裡有哪些卡。 |
| PostCompact | 5 秒 | 準備把那些卡送回去。 |
| PostToolBatch | 5 秒 | 壓縮後的開場沒送成時，由這裡送。 |
| Stop | 20 秒 | 知識收割（請 Claude 把學到的存成卡；同一場最多每兩次才擋一次）、評哪些注入的卡有用。 |
| SubagentStart | 5 秒 | 不是經 Agent 工具起的子代理也給卡。 |
| SubagentStop | 20 秒 | 評子代理用了哪些卡。 |
| StopFailure | 10 秒 | 回合因 API 錯誤結束時做記號，不把它算成「卡沒用到」。 |
| SessionEnd | 1.5 秒（Claude Code 對插件 SessionEnd hook 的上限） | 把收尾工作交給背景 worker 後立刻返回，關閉不會被拖住：知識萃取、互相矛盾的卡檢查、裝了向量搜尋就更新向量索引。 |

同一台機器上 git 安裝版還在跑時，每條 hook 都立刻結束、MCP 伺服器不登記任何工具，不會重複注入。開場一行說明怎麼切換（見「與 git 安裝版並存」）。

### MCP 伺服器

一個 stdio 伺服器 `workflow-guardian`（`node ${CLAUDE_PLUGIN_ROOT}/tools/workflow-guardian-mcp/server.js`），9 個工具：

| 工具 | 做什麼 |
|---|---|
| `atom_write` | 新增或更新一張卡（查重複與矛盾、更新索引）。 |
| `atom_promote` | 提高卡片的可信度等級。 |
| `atom_feedback` | 記一票：這張卡幫上忙或帶錯路。 |
| `atom_move` | 把卡搬到別的層或範疇。 |
| `atom_edit_meta` | 改卡片檔頭欄位。 |
| `atom_retire` | 讓卡退役。 |
| `knowledge_harvest_report` | 回報這場存了什麼，給 Stop 收割核對。 |
| `memory_search` | 用和每句注入相同的方式找卡。 |
| `atom_source` | 看卡片從哪來；原對話還在就引原句。 |

伺服器做事時會在本機起插件自己的 Python 模組。

### 本機背景工作

- hook 會起插件自己的腳本：重建索引、待辦追蹤檢查、補卡片建立日期、知識萃取 worker。
- 唯讀的 git／svn 查詢（status、log、ls-files、rev-parse、rev-list、check-ignore），只在專案與記憶資料夾是版控庫時：提醒卡片索引的合併衝突、還沒推出去的卡、補卡片日期。
- 專案裡有 `.claude/hooks/project_hooks.py` 時，開場會跑它（`python <檔> session_start`，5 秒上限）並顯示它回的行。這會執行專案資料夾裡的程式。
- 加入網域的 Windows 機器，開場會跑 `whoami /groups` 讀群組名，除非 `role.md` 已寫了你的職能。只有在設定裡填了群組對照表（`roles.ad_group_map`）才會把群組名對到職能（給組織層卡片用）；出廠是空表，所以預設指令會跑、但不會對到任何職能。群組名不出本機。
- Stop 時，只有這場改過 MCP 伺服器本身的程式才會列行程表（PowerShell `Get-CimInstance Win32_Process` 或 `ps`），判斷跑著的伺服器是不是舊版。
- 本機 LLM 萃取：有些 worker 會請 Ollama 幫忙：SessionEnd 的決策萃取、你說「又壞了」這類話之後的失敗模式萃取、寫卡時的矛盾檢查。預設位址是本機 `http://127.0.0.1:11434`。另外要有語言模型 `qwen3:1.7b`（`/memoryatoms:setup-vector` 第 4 步，或 `ollama pull qwen3:1.7b`）。那裡沒有 Ollama 回應、或沒有這個模型，就不萃取，什麼都不送；畫面上不說，每次失敗只在資料夾的 `Logs/atom-debug.log` 記一行 `ERROR`。SessionEnd 的全逐字稿萃取預設關。
- 向量服務：只在你跑過 `/memoryatoms:setup-vector` 之後。之前從不啟動。之後每場開場確認有一個本機 HTTP 服務在跑（只綁 `127.0.0.1`，埠由資料夾路徑推出，28000～28999），本機 Ollama 沒在跑就叫起來。設定 `vector_search.auto_start_service: false` 可關。

### 你自己跑的工具

技能（`/memoryatoms:memory`、`conflict`、`vector`、`refile`、`org`、`heal-review`、`behavior-preservation` 等）背後的命令列工具，只在你、或 Claude 照你要求用到時才跑。值得知道的兩個：`tools/health-weekly.py` 會呼叫 `claude -p ... --model haiku`（用你的 Claude 帳號）；`behavior-preservation` 的 `preserve-check.py` 會執行你給它的 shell 指令。插件版不排程這些工具。

## 讀什麼

| 什麼 | 誰讀、為什麼 |
|---|---|
| Claude Code 逐字稿 `<Claude 設定資料夾>/projects/<專案>/<session>.jsonl` | Stop 讀這場的尾段做知識收割與卡片評分；SubagentStart 讀最後一個工具呼叫；萃取 worker 讀這場最多 20,000 字；`atom_source` 打開卡片出處那場的逐字稿引原句。只讀。 |
| Claude Code `settings.json` | `autoMemoryEnabled`（給 `/memory` 建議用）、`pluginConfigs`（本插件設定）、使用者層／專案層／專案本機層的 `enabledPlugins`（哪些 MemoryAtoms 擴充開著）、hook 指令（偵測 git 安裝版）。只讀。 |
| `~/.claude.json` | `mcpServers`，偵測 git 安裝版的 MCP。只讀。 |
| Claude 設定資料夾的 `CLAUDE.md` | 是否已匯入 `IDENTITY.md`（已匯入就不重送）。只讀。 |
| 你的資料夾 | 卡片、索引、設定、狀態、紀錄、`IDENTITY.md`、`USER.md`。 |
| 你的專案 | `.claude/memory/`（專案卡）、`.claude/project-tree.json`（專案根宣告）、`.claude/hooks/project_hooks.py`（見上）、以前開過的其他專案的記憶資料夾（給標了跨專案的卡）。 |
| 作業系統使用者名 | 第一次填進 `USER.md`，也是個人卡的歸屬。 |

## 寫到哪

| 哪裡 | 寫什麼 | 什麼時候 |
|---|---|---|
| 資料夾（預設 `~/.memoryatoms`） | 卡片、索引、出廠卡與其紀錄 `_shipped.json`、session 狀態、紀錄、向量資料庫、`IDENTITY.md`、`USER.md`。 | 隨時。解除安裝插件後保留。 |
| `${CLAUDE_PLUGIN_DATA}` | `python.json`（找到的 Python）、`pycache/`；跑過 setup-vector 才有 `venv/` 與 `downloads/`。 | 第一次跑 hook；setup-vector。解除安裝時刪。 |
| `<專案>/.claude/memory/` | 專案卡與索引、`failures/`、`_staging/`；資料夾在 git／svn 裡時，把衍生索引檔加進 `.gitignore`（或 `svn:ignore`）。 | 你初始化專案記憶之後、卡片寫進專案時。例外：本機 LLM 萃取有作用時，草稿寫到 `<專案>/.claude/memory/personal/auto/<使用者>/`，就算沒初始化專案記憶也會建這個資料夾。 |
| 組織層資料夾 | clone、索引、組織層卡片。 | 只在你用 `/memoryatoms:org` 接上之後。 |
| Claude Code `settings.json` 與使用者層 MCP 登記 | 先備份到 `<Claude 設定資料夾>/backups/`，再拿掉 git 安裝版的 hook 與它的 `workflow-guardian` MCP 登記。 | 只在你跑 `/memoryatoms:migrate-to-plugin --apply`（或 `--undo`）時。 |

插件不寫自己的插件資料夾、不改 `~/.gitconfig`、不改 `settings.json` 也不改 MCP 登記；唯一例外是上面你自己啟動的遷移（它的 `--undo` 會把舊登記放回去）。

## 傳到哪

預設什麼都不傳：沒有遙測、沒有分析、不回報作者。只有你設了下面其中一項，資料才會離開這台機器：

| 什麼 | 傳到哪 | 怎麼開 |
|---|---|---|
| 對話摘錄、你的提問、卡片內容（上面的本機 LLM 萃取） | 遠端的 Ollama 相容伺服器 | 在 `<資料夾>/workflow/config.json` 的 `vector_search.ollama_backends` 加一個 `base_url` 不在本機的後端。可選登入：帳號密碼（安裝對話框的 `remote_llm_user`／`remote_llm_password`，或後端的 `auth` 設定）送到你設的 `login_url`，token 快取在 `<資料夾>/workflow/.ollama_token.json`。https 會驗憑證。 |
| 你的卡片（git push／pull） | 你記憶資料夾原本的遠端 | 開同步（`vcs_sync.enabled: true`）。插件版預設關。 |
| 組織記憶的 git clone／pull | 你設定的 repo 網址 | 用 `/memoryatoms:org` 接上組織層（預設關、網址空白）。 |

## 下載什麼

只有 `/memoryatoms:setup-vector`。不加 `--apply` 只印計畫。每一步先說下載什麼、多大、放哪，你點頭才做：

1. Python 套件：從 PyPI 裝釘死版本的 `lancedb` 與相依套件，下載約 100～130 MB（佔磁碟約 300 MB），裝進 `${CLAUDE_PLUGIN_DATA}/venv`。不動系統 Python。
2. Ollama（已裝就跳過）：從 Ollama GitHub 最新版下載官方安裝檔，先比對該版公布的 sha256（Windows 另驗 Authenticode 簽章）才執行。Windows：約 1.7 GB，靜默安裝到 `%LOCALAPPDATA%\Programs\Ollama`（只裝給你這個使用者）。macOS：16 KB 的 `install.sh`，再下載 App（約 230 MB）到 `/Applications/Ollama.app`。Linux：16 KB 的 `install.sh`，再下載 Ollama（大小事先不知道，含 GPU 函式庫約 1～2 GB）到 `/usr/local`，會要 sudo。
3. 嵌入模型：`ollama pull qwen3-embedding`，約 4.7 GB，由 Ollama 保存。
4. 萃取模型：`ollama pull qwen3:1.7b`，約 1.4 GB，由 Ollama 保存。session 結束時的知識萃取用它；沒有它就不萃取（見「本機背景工作」）。設了遠端 LLM 可以跳過。

不裝向量搜尋，卡片照樣靠觸發詞與關鍵字找得到。作者實測：換句話問，沒向量時 51.8% 找得到對的卡，有向量時 71.4%。

另一種情況：你自己把 `sentence-transformers` 裝進向量用的 Python（setup-vector 不裝），Ollama 不在時向量服務會改用它，而它會從 Hugging Face 下載 `BAAI/bge-m3` 模型。把 `vector_search.fallback_backend` 設成 `"none"` 可避免。

## 插件版預設關的

寫在插件的 `workflow/config.plugin.json`；你自己的 `<資料夾>/workflow/config.json` 或 `config.local.json` 可以打開。

| 設定 | 關掉＝ |
|---|---|
| `vcs_sync.enabled: false` | 不在背景對任何記憶資料夾 git commit、pull、push。卡片只留在本機。 |
| `merge_driver.auto_install: false` | 從不改 `~/.gitconfig` 安裝卡片索引合併驅動。 |
| `skill_index.enabled: false` | 不在背景重寫技能索引（會寫進插件資料夾）。 |
| `eol.auto_normalize_project: false` | 從不在專案 `.gitattributes` 加區塊、也不設專案檔的 `svn:eol-style`。 |
| `guard_family.enabled: false` | 寫碼守門家族（口令閘、跨層閘、驗證帳本、壞味道閘等）不跑。它只在完整版。 |

Claude Code 內建的自動記憶插件關不掉；見上面的 `/memory` 建議。

## 擴充

同一個市集裡另有 9 個選裝插件。每個都要靠 `memoryatoms`，裝了會一併啟用它。分開裝是為了讓核心保持精簡：要哪個裝哪個。

| 插件 | 加了什麼 | 安裝 |
|---|---|---|
| `memoryatoms-changelog-roll` | `_CHANGELOG.md` 超過門檻時在背景把舊列搬進 `_CHANGELOG_ARCHIVE.md`；`/memoryatoms-changelog-roll:changelog-debug` 手動跑。 | `/plugin install memoryatoms-changelog-roll@memoryatoms` |
| `memoryatoms-codex` | Codex 整合：`/memoryatoms-codex:codex` 第二意見與交辦施工，加上 Codex Companion 背景審查。 | `/plugin install memoryatoms-codex@memoryatoms` |
| `memoryatoms-coordination` | 多 session 協作：另一個還開著的 session 改同一個檔、或要收尾動 git 時提醒（只提醒，不擋）。 | `/plugin install memoryatoms-coordination@memoryatoms` |
| `memoryatoms-dashboard` | MCP 行程內的本機網頁儀表板：dashboard、brain-world、收尾報告 HUD。 | `/plugin install memoryatoms-dashboard@memoryatoms` |
| `memoryatoms-handoff` | 交接單：壓縮前與 session 結束時備好 `next-phase-auto.md`、context 快滿時提醒，並提供 `/memoryatoms-handoff:handoff`。 | `/plugin install memoryatoms-handoff@memoryatoms` |
| `memoryatoms-statusline` | 狀態列腳本：改過的檔、知識佇列、向量服務、收尾報告（不花 token）。沒有 hook、沒有 MCP，要你自己打開（見下方）。 | `/plugin install memoryatoms-statusline@memoryatoms` |
| `memoryatoms-usage-watch` | Claude 帳號用量監控：額度檢查、異常通知，監控沒在跑時開場提示一行。 | `/plugin install memoryatoms-usage-watch@memoryatoms` |
| `memoryatoms-wisdom` | 反思統計：記錄每場 session 的狀況，開場最多兩行自我提醒。 | `/plugin install memoryatoms-wisdom@memoryatoms` |
| `memoryatoms-workflow-nudge` | 工作方式提示：研究型請求建議分頭查，互不相干的工作建議並行子代理。 | `/plugin install memoryatoms-workflow-nudge@memoryatoms` |

### 狀態列

Claude Code 的插件不能設定狀態列，所以 `memoryatoms-statusline` 只帶腳本。裝好後，把那個插件 README 裡的 `statusLine` 設定加進你的使用者 `settings.json`；要關掉就刪掉這個鍵。

## 與 git 安裝版並存

一台機器只跑一套。`settings.json` 裡有 git 安裝版的 hook，或 `~/.claude.json` 登記了它的 `workflow-guardian` MCP 伺服器時，插件版讓路，每場說一次。要切換就跑 `/memoryatoms:migrate-to-plugin`：

1. 預覽（預設）：列出會拿掉哪些 hook 條目、哪個 MCP 登記。什麼都不改。
2. 你同意後 `--apply`：把 `settings.json` 備份到 `<Claude 設定資料夾>/backups/migrate-<時間>/`，只拿掉 git 安裝版自己的 hook 條目（其他 hook 留著），再跑 `claude mcp remove workflow-guardian -s user`。
3. `--undo`：從備份逐位元組還原 `settings.json`，並重新登記 MCP 伺服器。

卡片、索引、紀錄、`CLAUDE.md` 與 workflow 設定一律不動。把插件的 `data_dir` 設成 `~/.claude` 就能接著用原本的卡。

## 解除安裝

`/plugin` 會移除插件與 `${CLAUDE_PLUGIN_DATA}`。你的資料夾（預設 `~/.memoryatoms`）留著；不要那些卡了再自己刪。

## 機器可讀清單

`disclosure.json` 逐項列出上面每件事：什麼時候發生、在哪、原始碼哪一檔哪一行做的。
