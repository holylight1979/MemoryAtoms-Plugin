# 核心規則

> 本檔只放事前就要知道、或沒有程式閘會擋的規則；程式已會擋或會提示的事不重述。

## 系統自我認知
- 記憶系統的目標：知識與經驗全部積累、分門別類，需要的那一刻完整送到，不相關的不佔 token。
- **記憶三層**（scope：寫卡時決定這張卡給誰看）：根層 `~/.claude/memory/` 放跨專案通用的 → `global`；組織層（接上的組織記憶 repo）放全組織共用的 → `org`；專案層 `{專案}/.claude/memory/` 放該專案的決策與踩坑 → `shared`／`personal`。
- **根層只在根層改**：`~/.claude` 的系統檔只在 `~/.claude` 的 session 改；專案 session 遇到就把需求寫成可貼上的 prompt 交給使用者。跑根層工具不算改：用絕對路徑跑記憶系統的 `tools/<工具>.py`（hook 印的提示行會給完整指令），不 `cd` 進去。
- **可觀測性鐵律**：不阻斷的失敗（fail-open）也要告知——降級或靜默失敗必浮出訊號（告警、stderr、收尾報告），不無聲吞掉。

## 知識庫
- 禁憑記憶改碼；斷言嚴重度或「必爆」前先實證（跑、查、追根源），細節見 [[feedback-未實證先別斷言-從根源驗證-先證再修-反退避反冗長]]。
- `_AIDocs`（專案文件庫）只放長期參考；規劃、TODO、進行中的事放 `memory/_staging/`。
- 程式、設定、測試與卡片只寫現況，不埋版本、日期、變更敘事；編年放 `_AIDocs/DevHistory/` 與 `_CHANGELOG.md`。
- 寫規則或決策：只把目標寫成一律，做法寫「目前用 X」；退版或否決寫明是方向錯，還是時機或資源不夠。

## 記憶
- 使用者說「初始化原子記憶庫」或開場出現 `[Guardian:ProjectMemory]` → 照提示行給的指令跑，不自己手建目錄。
- 寫卡目前走 atom_write MCP（會驗證、去重、建索引），不直接改卡片 .md；新卡目前以 [臨] 起跳，晉升靠使用或 append；create 必給 `domain`（清單在 `memory/_meta/taxonomy.json`），落點拿不準先 `dry_run`，分不出範疇的不寫。已記錄的事實直接引用，不寫臨時嘗試或未確認的猜測。
- **Scope** 問「遷就專案還是遷就人」：專案的規則與決策 → `shared`；只關於這個人 → `personal`；本人跨專案的偏好 → `personal, cross_project=true`；全組織所有專案都該知道 → `org`。
- **Realm**（卡放哪一區）：跨專案通用 → `memory/<範疇>/`；只在 `~/.claude` 內有用 → `realm=local`。判定三問：可重用到兩個以上專案嗎？是系統規則，還是特定 app、工具、環境的事？有效期是以月計還是以週計？偏前者 → core，偏後者 → local；拿不準回 core。
- **個人隱私敏感資訊放 secret**（密碼、token、私鑰、憑證、個資、內網位址等不想公開的）：`atom_write` 帶 `secret=true`，卡裡只寫值放在哪、不寫值本身；只是一般技術知識就帶 `not_sensitive=<理由>`；判不準當敏感（藏了再搬出來容易，公開了收不回）；一張卡只有部分隱私就拆成兩張；放錯區的卡用 `atom_move` 搬。
- **清單不進卡**：待辦、backlog、稽核結果、對照表這類會一直加項目的，正本寫 `memory/_staging/<主題>.md`，卡只留指標；卡片整顆注入且有預算，長清單會被截斷。卡被預算擋下時先分辨是結論還是清單：結論就壓縮，清單就換形狀，不靠刪既有內容換空間。
- Failures 卡（踩坑紀錄）寫禁令時，同行或下一行寫「情境：」說明何時適用、何時不適用。
- 注入的卡明顯救了你或帶錯你 → `atom_feedback(atom_name, verdict=helpful|misleading)` 投一票；沒真的用到的不投。

## 版控
- 記憶卡片與索引由系統背景同步上傳（若你開了同步），AI 不碰也不問。
- 程式與文件等使用者口令（含原子記憶系統本身的）：改完只報告改了什麼、驗了什麼，等使用者看 diff 下版控口令（預設如「上GIT」，清單在 `workflow/config.json` 的 `commit_order.keywords`）再 commit＋push。

## 對話
- 「用識流…」→ /memoryatoms:consciousness-stream；/resume → /memoryatoms:continue。
- 獨立子任務可新開對話，拆前先把知識存好；對話快被壓縮時提醒使用者開新 session。
- Plan Mode 查讀目前用 Read / Grep / Glob 工具，不用 Bash 的 cat/grep/sed；Bash 指令直接寫絕對路徑，不加 `cd` 前綴。
- 中大型、可拆的工作（至少兩份不寫同一批檔、每份都要讀很多檔或做好幾分鐘）就自己開 agent 並行，不必先問；每份明填 model，見技能 parallel-dispatch。檢索型請求的拆法見 [[workflow-research-fanout]]。
