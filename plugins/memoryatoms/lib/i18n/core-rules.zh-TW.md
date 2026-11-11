[Guardian:CoreRules] 怎麼寫記憶卡
- 使用者說「初始化原子記憶庫」或開場出現 `[Guardian:ProjectMemory]` → 跑 `python {code_root}/tools/init-project-memory.py --cwd <專案根>`，不自己手建目錄。
- 寫入一律走 atom_write MCP（自動驗證去重索引），不直接編輯卡片 .md；已記錄的事實直接引用；不寫臨時嘗試或未確認的猜測。新卡一律 [臨] 起跳（系統拒收新建 [固]／[觀]），晉升靠使用或 append。注入的卡明顯救了你一次或把你帶錯 → `atom_feedback(atom_name, verdict=helpful|misleading)` 投一票（沒真的用到的卡不投）。
- Realm：跨專案通用 → `memory/<範疇>/`（失敗家族 `memory/Failures/<主題>/`）；只在記憶系統自己的資料夾內有用 → `realm=local`。
- Scope（問「遷就專案還是遷就人？」）：專案的規則／決策 → `shared`；只關於這個人 → `personal`；本人跨專案偏好 → `personal, cross_project=true`；整個組織所有專案都該知道 → `org`。
- 個人隱私敏感資訊放 secret（密碼、token、私鑰、憑證、個資、內網位址、任何不想公開的驗證或隱私內容）：`atom_write` 帶 `secret=true`，落 `memory/personal/<user>/secret/`——不進版控、不送向量、只給本人、觸發詞照常注入。其他個人卡（工作偏好）照常進版控，不必躲。值本身別寫進卡，只寫放在哪。寫入閘碰到相關字眼會要你判斷：只是一般技術知識就帶 `not_sensitive=<理由>`；判不準當敏感（放 secret 之後搬出來很容易，公開了收不回）。一張卡部分隱私、部分不是 → 拆成兩張，隱私那半才進 secret；寫入閘會擋整張沒隱私字眼、或只有部分行命中的 secret 卡，整張確實都該藏才帶 `secret_reason=<理由>`。已經寫錯地方的卡用 `atom_move` 搬進去（或搬出來）。
- create 必給 `domain`（Lv1 閉合清單 `memory/_meta/taxonomy.json`），不確定落點先 `dry_run`；分不出範疇的知識不寫。
- 清單不進卡：待辦／backlog／稽核結果／對照表這類「會一直加項目」的東西，正本寫 `memory/_staging/<主題>.md`，卡只留指標（正本在哪／怎麼改／什麼不寫這裡）。卡隨 trigger 整張注入、知識段預設約 200 tok（預算夠時最多 400），長清單仍會被截斷——放進去等於記了但送不到。寫入閘以待辦標記數硬拒（`- [ ]`／`- [x]` ≥3）。
- 卡被預算擋下時先分辨這是結論還是清單：結論 → 壓縮（個案敘事移文件）；清單 → 換形狀。別靠刪既有內容換空間。放寬寫入預算沒用，注入端會先截斷。
