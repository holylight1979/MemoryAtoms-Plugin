## 原子記憶：怎麼寫卡
- 寫卡一律走 atom_write MCP（自動驗證、去重、索引），不直接改卡片 .md；已記錄的事實直接引用，不寫臨時嘗試或未確認的猜測。
- 新卡一律 [臨] 起跳，晉升靠使用或 append，不自己標。
- create 必給 `domain`（清單在 `memory/_meta/taxonomy.json`）；不確定落點先 `dry_run`。
- Scope（遷就專案還是遷就人？）：專案的規則／決策 → `shared`；只關於這個人 → `personal`；本人跨專案偏好 → `personal, cross_project=true`；全組織所有專案都該知道 → `org`。
- Realm：可重用 ≥2 個專案、是系統規則、月級有效 → core；特定 app／工具／環境、週級有效 → `realm=local`；拿不準回 core。
- 隱私（密碼、token、私鑰、憑證、個資、內網位址）→ 帶 `secret=true`，卡裡只寫值放在哪、不寫值本身；只是一般技術知識帶 `not_sensitive=<理由>`；判不準當敏感；一張卡部分隱私就拆成兩張。
- 清單不進卡（待辦、backlog、稽核結果、對照表）：正本寫 `memory/_staging/<主題>.md`，卡只留指標；卡片整顆注入，長清單會被截斷。
- 注入的卡明顯幫到或帶錯你 → `atom_feedback(atom_name, verdict=helpful|misleading)`。
