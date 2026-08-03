# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

專案說明文件為 [README.md](README.md)（繁體中文，含完整架構圖、DB schema、環境變數表）。本檔只補充 README 沒寫、但改動程式碼前必須知道的事。

## 常用指令

```bash
pip install -r requirements.txt
playwright install chromium
```

| 目的 | 指令 |
| :--- | :--- |
| 本地跑「排程 + 儀表板」（正式本地入口） | `python main.py` → http://localhost:5000 |
| 只跑 Flask 雲端版儀表板（本地驗證用） | `python app.py` |
| 只跑一次爬蟲、印出前 3 筆結果（不寫 DB） | `python scraper.py` |
| 跑一次完整巡邏：爬取 → 寫 DB → 發 Discord | `python run_crawler_standalone.py` |
| 檢查 `.env` 解析出的目標網址清單 | `python search_filters.py` |
| 用假資料測 Discord 卡片版型（webhook 留空＝只印 log） | `python notifier.py` |
| 生產環境啟動（Render / Procfile） | `gunicorn app:app --workers 1 --worker-class gthread --threads 4 --timeout 300 --keep-alive 5` |

**本專案沒有測試框架**（`requirements.txt` 無 pytest，也沒有 `test_*.py`）。根目錄的 `test_*.db` 只是舊的手動測試殘留檔。驗證改動請用上表的單檔執行方式。

## 架構重點（改動前必讀）

### 1. 兩個入口共用 `ui_shared.py` 的前端與格式化邏輯

- `app.py`（Flask，雲端）與 `dashboard.py`（`http.server`，本地）都從 [ui_shared.py](ui_shared.py) 取得 `HTML_TEMPLATE` 與 `get_formatted_houses(db, scraper)`，各自只保留一個 `PAGE_TITLE` 常數。**改 UI 或卡片欄位只要改 `ui_shared.py` 一處。**
- `get_formatted_houses()` 帶 5 秒記憶體快取（`CACHE_TTL_SECONDS`），並在此處才即時注入 `cost_info` / `couples_warnings` / `couples_features`——這些欄位刻意不落 DB，所以切換 `.env` 的 `MODE` 不需要重建資料庫。
- 寫入評分後要呼叫 `invalidate_houses_cache()`，否則使用者最多等 5 秒才看到新狀態。
- `HTML_TEMPLATE` 是**非 raw** 三引號字串，內含 `\\`（3 處）與 `\uXXXX`（10 處）。改動時若要加反斜線，記得無效跳脫要寫成 `\\s` 這種形式，別改成 `r"""`（`\\` 的語意會變）。

### 2. GitHub 是真正的 Master DB，SQLite 只是本地快取

`db.py` 的同步鏈是整個系統最脆弱的部分：

- `restore_from_backup_json()` 在 **每次 `HousingDB()` 建構時**（`_init_db()` 結尾）都會呼叫 —— 它會先從 `raw.githubusercontent.com` 下載並**覆寫本地 `rentals_backup.json`**，再把 DB 裡不存在的 `house_id` 補回 SQLite（只 INSERT，不 UPDATE，所以本地既有評分不會被雲端蓋掉）。
- `sync_backup_json()` 用模組層級的 `_LAST_PUSHED_HASH` 做 MD5 閘門：hash 沒變就完全不寫檔、不打 API。**破壞這個比較邏輯會造成 GitHub API 被高頻寫爆。**
- 兩道資料安全防線，改 `db.py` 時不要拆掉：
  1. `restore_from_backup_json()` 只有在讀到「可解析且非空」的主檔時才把 `self._master_loaded` 設為 True；`sync_backup_json()` 在它是 False 時會拒絕回推（先自動補讀一次，補不到就整個放棄）。這擋的是「Render 重新部署 → 抓不到備份 → DB 空的 → 把沒有評分的資料推上去覆蓋雲端」。
  2. `_LAST_PUSHED_HASH` **只在 `_push_to_github_api()` 回傳 True（HTTP 200/201）之後才更新**。若在推送前就更新，一次靜默失敗會讓那批資料永遠不再重試。409/422 會重抓 sha 重試一次。
- 寫回走 `PUT /repos/eason890725/OurHome/contents/rentals_backup.json`，需要環境變數 `GITHUB_TOKEN`；沒 token 就只寫本地檔。GitHub repo 名稱寫死在 `db.py` 頂端的 `GITHUB_REPO`。
- `process_houses_batch()` 結尾會呼叫 `checkpoint_wal()` 做 `PRAGMA wal_checkpoint(TRUNCATE)`。WAL 模式不會自動縮小 `-wal` 檔（只重複使用高水位空間），沒有這步 `rentals.db-wal` 會長期停在數 MB。有其他連線佔用時它會安靜跳過，不影響巡邏。

### 3. `rentals_backup.json` 必須保持被 git 追蹤

**不要對它執行 `git rm --cached` 或加進 `.gitignore`。** 它是雲端 Master DB 的實體：Render 用 GitHub API 把它 PUT 進 repo，所有節點再從 `raw.githubusercontent.com` 讀回。untrack 並 commit 會把它從 repo 上刪掉，`restore_from_backup_json()` 的 raw URL 直接 404。

防止本地舊資料覆蓋雲端的主要機制是 **`.githooks/pre-commit`**：它會自動把該檔從每次 commit 剔除（逃生門是 `OURHOME_ALLOW_BACKUP_COMMIT=1`）。需要 `git config core.hooksPath .githooks` 啟用，重新 clone 後要再設定一次。`main.py` 的 `auto_git_pull()` 也會在本地巡邏前 `git checkout` 丟棄本地變更，但只在跑 `python main.py` 時生效。

資料若出事可從 git 歷史還原：每次雲端 auto-sync 都是一個 commit，`git log --oneline -- rentals_backup.json`。

### 4. 爬蟲是純 HTTP，**不要再引入瀏覽器**

591 的搜尋結果頁是 Nuxt SSR，欄位都在 HTML 裡，`parse_list_html()` 直接解析 `div.item` 就能取得標題／租金／坪數／樓層／結構化地址／最近捷運站與距離／額外費用。

- 曾經用 Playwright 擷取 DOM，實測 Chromium 在 591 頁面上吃掉 300~400MB，加上 Web 程序突破 Render 512MB 上限，造成連續數日的 `Ran out of memory`。改純 HTTP 後 Python 峰值只有 21.7MB。**任何「改回瀏覽器」的提案都要先確認記憶體預算。**
- `MAX_LIST_PAGES`（預設 3）控制每個搜尋網址翻幾頁，591 每頁 30 筆。某頁沒有新物件就提早停止。
- 卡片提供的 `mrt_station` / `mrt_distance` 比從標題猜可靠，`commute.estimate()` 會優先採用（見第 5.5 節）。
- `requirements.txt` 已移除 playwright，Dockerfile 也改回 `python:3.11-slim`。**Render 的 Build Command 若還有 `playwright install chromium` 要一併移除**，否則每次 build 都白下載 150MB。

### 5. 雲端巡邏是獨立子程序，不是執行緒

`app.py` 模組載入時就 `crawler_thread.start()` 啟動 `background_crawler_loop()`，該迴圈用 `subprocess.Popen([sys.executable, "run_crawler_standalone.py"])` 開獨立進程巡邏，避開 GIL 與 Web 請求互鎖。因為是模組層級啟動，`gunicorn` 必須維持 `--workers 1`，否則每個 worker 都會各開一份爬蟲。

### 5.5 通勤估算是離線的，不要改成呼叫外部 API

[commute.py](commute.py) 內建台北捷運路網（`MRT_LINES`），用 Dijkstra 算最短路徑：每站 2 分、轉乘 5 分、步行 5 分。**使用者明確不要需要 API 金鑰的方案**（先前用 Google Distance Matrix 的版本就是因此被放棄的），所以不要再引入 Google Maps／TDX 之類需要註冊金鑰的服務。

- `find_station()` 用最長匹配從文字找站名，優先序是「有捷運字樣 > 出現位置較前 > 站名較長」。位置優先於長度是刻意的：`estimate()` 先看標題再退回 `details_text`，內文常順帶提到別的車站。
- `DISTRICT_COLLISIONS` 擋掉「中山區」被當成中山站這類誤判；站名後接「路街巷弄段號」也會被跳過（例如「南京東路」）。
- 路網未收錄環狀線。若要修正站名或順序，直接改 `MRT_LINES`，其餘程式碼不需要動。
- 這是**估算值**，UI 上有標註。實測 166 筆真實資料可辨識 165 筆（99%），其中 124 筆站名來自標題。

### 5.7 標記類欄位一定要納入備份還原

`houses` 有兩個「標記」欄位：`duplicate_of`（重複刊登指向的主物件）與 `excluded_by`（命中的排除關鍵字）。兩者都採「標記而非刪除」，資料仍在庫裡，只是不進儀表板列表。

**`restore_from_backup_json()` 的 INSERT 必須包含它們。** 曾經漏掉 `duplicate_of`，結果每次容器重啟從備份還原就把去重結果清空；而重建標記原本只在巡邏結束時進行，巡邏又一直被重啟中斷，於是儀表板永遠都是未去重的狀態（273 筆裡有 87 筆是重複的）。

因此 `_init_db()` 在 restore 之後會立刻呼叫 `apply_exclude_keywords()` 與 `dedupe_existing()` 重建標記，不依賴巡邏是否跑完。

排除關鍵字由 `config.py` 從環境變數讀取：`EXTRA_EXCLUDE_KEYWORDS` 追加、`EXCLUDE_KEYWORDS` 完全取代。`apply_exclude_keywords()` 會回溯處理既有資料，關鍵字拿掉時也會自動解除標記。

### 6. 通知的費用欄位由呼叫端負責填

`notifier.send_house_card()` 只做排版，它讀 `house["cost_info"]`，不會自己算。**兩條巡邏路徑都必須在發送前先跑 `parse_rental_costs()`**：`run_crawler_standalone.py`（雲端 24H 實際路徑）與 `main.py`（本地路徑）。漏掉的話卡片費用欄位會顯示「未估算」。

算 `full_text` 時務必包含 `details_text`——內頁抓回來的費用行都在那裡，只用 title + address 會解析不到管理費與電費。

`notify_new_house()` / `notify_price_drop()` 只是 `send_house_card()` 的包裝；後者是唯一真正發送的實作。

### 7. 去重與費用解析的「防呆上限」是刻意的

- `is_precise_duplicate()` 的核心設計：**地址只用來「排除」，不用來「認定」**。
  - `address_verdict()` 解析出 區/路/段/巷/弄/號 後，任一層級衝突就回 `conflict` 直接否決（例如新生北路二段 vs 三段）。巷/弄/號只在兩邊都標示時才比較，單邊缺漏視為粒度差異而非衝突。
  - 路徑 1（地址相容）：`price_diff <= 500` 且 `size_diff <= 0.5`。門檻刻意比路徑 2 嚴格，因為這條路徑不看標題，只靠價格坪數認定。
  - 路徑 2（標題相似）：`price_diff <= 1500`、`size_diff <= 1.5`，仲介 > 0.60 / 一般 > 0.70。
  - **調鬆任何門檻都會誤刪合法物件**——命中重複的房源會被 `process_house()` 直接回 `IGNORE`，永遠不會入庫也不會出現在儀表板上。改動前先跑 `python tests/test_dedup.py`，裡面的反例（同棟大樓不同戶、同路不同段）就是防這個。
- `cost_calculator.py` 每個欄位都有金額上限防呆（管理費 ≤ 10000、電費單價 2~10 元/度、水費 ≤ 2000、雜費 ≤ 2000），用來擋 regex 誤抓到租金或押金。改 regex 時要保留這些邊界檢查。

### 8. 模式切換是全域副作用

`config.py` 在 import 時就依 `MODE`（`couple` / `single`）決定 `DEFAULT_ELECTRICITY_KWH`、`MIN_SIZE_SQFT`、`EXCLUDE_KEYWORDS`。改 `.env` 的 `MODE` 會同時影響爬蟲過濾門檻與費用估算，不只是電費度數。
