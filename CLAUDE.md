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

### 4. 爬蟲的兩階段記憶體策略不能破壞

`scraper.py` `fetch_single_url()`：階段一 Playwright 只做 DOM 擷取，抓完立刻 `browser.close()` + `gc.collect()`；階段二完全用 `requests` 打 591 內頁。**不要把內頁抓取搬回 Playwright 階段內**，那會讓 Render 512MB 容器 OOM。`context.route` 阻擋 image/font/media/stylesheet 也是省記憶體的關鍵。

### 5. 雲端巡邏是獨立子程序，不是執行緒

`app.py` 模組載入時就 `crawler_thread.start()` 啟動 `background_crawler_loop()`，該迴圈用 `subprocess.Popen([sys.executable, "run_crawler_standalone.py"])` 開獨立進程巡邏，避開 GIL 與 Web 請求互鎖。因為是模組層級啟動，`gunicorn` 必須維持 `--workers 1`，否則每個 worker 都會各開一份爬蟲。

### 6. 通知的費用欄位由呼叫端負責填

`notifier.send_house_card()` 只做排版，它讀 `house["cost_info"]`，不會自己算。**兩條巡邏路徑都必須在發送前先跑 `parse_rental_costs()`**：`run_crawler_standalone.py`（雲端 24H 實際路徑）與 `main.py`（本地路徑）。漏掉的話卡片費用欄位會顯示「未估算」。

算 `full_text` 時務必包含 `details_text`——內頁抓回來的費用行都在那裡，只用 title + address 會解析不到管理費與電費。

`notify_new_house()` / `notify_price_drop()` 只是 `send_house_card()` 的包裝；後者是唯一真正發送的實作。

### 7. 去重與費用解析的「防呆上限」是刻意的

- `db.py` `is_precise_duplicate()` 的門檻（坪數差 > 1.5 直接排除、價差 > 1500 直接排除、相同地址需坪數差 ≤ 0.5、仲介標題相似度 > 0.60、一般 > 0.70）調鬆會誤刪合法物件。
- `cost_calculator.py` 每個欄位都有金額上限防呆（管理費 ≤ 10000、電費單價 2~10 元/度、水費 ≤ 2000、雜費 ≤ 2000），用來擋 regex 誤抓到租金或押金。改 regex 時要保留這些邊界檢查。

### 8. 模式切換是全域副作用

`config.py` 在 import 時就依 `MODE`（`couple` / `single`）決定 `DEFAULT_ELECTRICITY_KWH`、`MIN_SIZE_SQFT`、`EXCLUDE_KEYWORDS`。改 `.env` 的 `MODE` 會同時影響爬蟲過濾門檻與費用估算，不只是電費度數。
