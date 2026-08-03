# OurHome - 591 租屋品質與真實成本自動化監控儀表板

> **專案版本**：v2.5 (24H 雲端零成本全自動巡邏 & 2 秒脫離極速低記憶體架構)  
> **適用平台**：Render Cloud (雲端 24H 免費部署) / Windows 本地獨立執行  
> **專用說明檔**：本 README 包含專案之完整架構設計、資料流動圖、資料庫 schema、記憶體優化原理、去重演算法與環境變數設定，供 Claude 或其他 AI 輔助開發者全盤了解。

---

## 📖 1. 專案簡介 (Project Overview)

**OurHome** 是一款針對台灣 591 租屋網（`rent.591.com.tw`）設計的自動化租屋品質監控與真實月成本估算系統。

### 🌟 核心解決痛點：
1. **真實月總成本計算**：591 刊登租金不包含管理費、台電/非台電高額電費、水費與垃圾代收費。OurHome 自動解析內文，估算雙人模式（預設 400 度電）或單人模式（預設 200 度電）的真實預估月支出。
2. **100% 精準下架/出租辨識**：物件被租出下架後，系統自動將卡片加上 `🏚️ 已下架/已出租` 灰色標籤並提供獨立篩選，**絕不抹滅使用者對該房源的歷史評分與紀錄**。
3. **雲端 512MB RAM 極致省記憶體**：獨創「二階段極速 2 秒脫離 Playwright 架構」，Chromium 瀏覽器僅存活 2 秒擷取 DOM 即刻銷毀，整體運作記憶體死死控制在 **< 40MB**，徹底解決 Render 512MB 免費伺服器 OOM (Out of Memory) 崩潰問題。
4. **雙向無衝突全自動雲端同步**：利用 GitHub REST API，Render 雲端伺服器在巡邏到新房源或收到使用者評分時，自動 commit 最新資料回 GitHub，且 `.gitignore` 排除本地 JSON 覆蓋，實現本地推播程式碼也不會覆蓋雲端 DB 的零衝突架構。
5. **多網址搜尋與同義字搜尋儀表板**：前端網頁儀表板支援行政區動態標籤、同義字自動連動搜尋（輸入「租補」可自動連動「社宅/租金補貼/可補/補助」）。

---

## 🏗️ 2. 系統架構與技術棧 (Architecture & Tech Stack)

```
                       [ 591 租屋網 (rent.591.com.tw) ]
                                      │
              ┌───────────────────────┴───────────────────────┐
              ▼ (Playwright DOM 擷取 2 秒銷毀 + 純 Python HTTP 內頁)
  [ ☁️ Render Cloud Web Service ]               [ 💻 本地 PC / Standalone ]
   - Flask Web / Dashboard (Port 5000)            - Standalone Python / Dashboard
   - 24H 背景獨立巡邏子程序                         - 選擇性本機巡邏
   - SQLite (WAL 模式高併發)                       - SQLite (WAL 模式)
              │                                               │
              └───────────────────────┬───────────────────────┘
                                      ▼
                      [ 🐙 GitHub REST API (Master DB) ]
                            rentals_backup.json
                                      │
                                      ▼
                        [ 📣 Discord Webhook 頻道 ]
                    (自動標註 [☁️ Render 雲端] / [💻 本地 PC])
```

- **程式語言**：Python 3.11+ / JavaScript (Vanilla ES6 HTML5 Dashboard)
- **Web 框架**：Flask (雲端) / `http.server.HTTPServer` (本地免安裝單檔)
- **網頁爬蟲**：Playwright (Chromium Headless) + `requests` (純 HTTP 明細擷取)
- **資料庫**：SQLite 3 (`PRAGMA journal_mode=WAL;` 高併發模式)
- **伺服器 WSGI**：Gunicorn (`gthread` 多執行緒模式, timeout 300s)
- **即時通知**：Discord Webhook API (內建 HTTP 429 Retry-After 避讓與來源標籤)
- **雲端同步**：GitHub REST API (`PUT /repos/{owner}/{repo}/contents/{path}`)

---

## 🔄 3. 全自動資料流動與同步機制 (Data Flow & GitHub Sync)

### A. 爬蟲巡邏流向
```
[ 591 搜尋頁面 ] ──(Playwright 2秒)──> [ 記憶體內卡片清單 ] ──(Chromium 關閉)
                                             │
                                             ▼ (純 Python HTTP requests.get)
                                    [ 591 內頁與下架狀態比對 ]
                                             │
                                             ▼
                                 [ SQLite DB (rentals.db) ]
                                             │
                                             ▼ (MD5 指紋 Hash 比對)
                                   Hash 是否變動?
                                ├── 是 ──> [ rentals_backup.json ] ──(GitHub API)──> [ GitHub Repo ]
                                └── 否 ──> (靜音不發送 API，避免請求浪費)
```

### B. 防止本地推播覆蓋雲端 DB 之關鍵設定

> ⚠️ **`rentals_backup.json` 必須保持被 git 追蹤，絕對不可以 untrack。**
> 它就是整套系統的 Master DB：Render 雲端用 GitHub API 把它寫進 repo，所有節點再從 repo 讀回。
> 若執行 `git rm --cached rentals_backup.json` 並 commit，該檔會從 GitHub repo 上被刪除，
> `restore_from_backup_json()` 的 raw URL 會 404，等同雲端資料庫全毀。

實際的防覆蓋機制有三層：

**第一層 — `.githooks/pre-commit`（本地推程式碼時）**
自動把 `rentals_backup.json` 從每次 commit 剔除，`git add .` / `git commit -a` 也不會誤傷。
重新 clone 之後需要啟用一次：

```bash
git config core.hooksPath .githooks
```

真的要提交它時（例如從 git 歷史還原資料）：`OURHOME_ALLOW_BACKUP_COMMIT=1 git commit -m "..."`

**第二層 — 讀不到 Master 就禁止回推（`db.py`）**
Render 免費方案硬碟是暫存的，每次重新部署 `rentals.db` 都會被清空、靠雲端備份還原。
若開機時因網路問題抓不到備份，DB 會是空的；此時若照常回推，就會用一份「有房源但沒有任何評分」
的資料覆蓋掉正確版本。因此 `restore_from_backup_json()` 只有在確實讀到可解析且非空的主檔時，
才會把 `_master_loaded` 設為 True，否則 `sync_backup_json()` 一律拒絕回推（並會先自動補讀一次）。

**第三層 — 推送成功才記錄 hash（`db.py`）**
`_LAST_PUSHED_HASH` 只在 GitHub 回傳 200/201 之後才更新。推送失敗會保留舊 hash，
下次同步自動重試；遇到 409/422（sha 過期）會重抓 sha 再試一次。

**若資料真的出事**：每次雲端 auto-sync 都是一個 commit，
`git log --oneline -- rentals_backup.json` 可以找到任一時間點還原。

此外 `main.py` 的 `auto_git_pull()` 在本地巡邏前會先 `git checkout rentals_backup.json`
丟棄本地變更再 pull（僅在本地執行 `python main.py` 時生效）。

- 雲端 Render 伺服器與本地開機時，`db.restore_from_backup_json()` 會透過 GitHub REST API / Raw URL 直接下載最新備份檔並還原至 SQLite（只 INSERT 缺漏的 `house_id`，不 UPDATE，因此本地既有評分不會被蓋掉）。
- 本地若要手動推程式碼，請避免 `git commit -a`；用 `git add <指定檔案>` 明確列出要提交的檔案。

---

## ⚡ 4. 記憶體與爬蟲效能優化 (Memory & Scraper Optimization)

### A. 完全不使用瀏覽器 ([scraper.py](file:///c:/personl/OurHome/scraper.py))

591 的搜尋結果頁是**伺服器端渲染**（Nuxt SSR），所有欄位都直接寫在 HTML 裡，
因此不需要 Playwright／Chromium：

1. `requests.get()` 取回列表頁 HTML，用 `&page=N` 翻頁（預設 3 頁，`MAX_LIST_PAGES` 可調），
   某一頁完全沒有新物件就提早停止。
2. `parse_list_html()` 以 `div.item` 切出每張卡片，解析出
   標題／租金／坪數／樓層／**結構化地址**／**最近捷運站與距離**／**額外費用**。
3. 再用純 HTTP 打內頁補充費用細項並驗證下架狀態。

> 曾經的做法是啟動 Chromium 擷取 DOM 後立即關閉。但 Chromium 在 591 這種重度頁面上
> browser + renderer 通常吃掉 300~400MB，加上 Web 程序就會突破 Render 免費方案的 512MB，
> 實際造成連續數日的 `Ran out of memory`。純 HTTP 之後這個問題從根本消失。

### B. 結果

| | Playwright 版 | 純 HTTP 版 |
| :--- | ---: | ---: |
| Python 記憶體峰值 | — | **21.7 MB** |
| Chromium 額外開銷 | 300~400 MB | **0** |
| 單輪抓取筆數（2 個網址） | ~50 | **139** |
| 管理費解析成功率 | ~25% | **70%** |
| 取得最近捷運站 | 靠標題猜 | **100%（591 直接標示）** |
| Docker 映像檔 | ~1.5 GB | ~150 MB |

SSR 的 HTML 反而比 DOM 擷取提供更多欄位，因此準確度也一併提升。

---

## 🏚️ 5. 下架/出租與去重邏輯 (Off-market & Deduplication)

### A. 已下架/已出租判斷
- 直連 591 內頁 URL (`https://rent.591.com.tw/{house_id}`)。
- 若 HTTP Status Code 為 `404` 或 `410` ➔ 標記 `status = 'off_market'`。
- 若 HTTP Status Code 為 `200`，但 HTML 內文包含以下關鍵字 ➔ 標記 `status = 'off_market'`：
  - `"您查詢的物件不存在"`、`"可能已關閉或者被刪除"`、`"物件已下架"`、`"已被租出"`、`"找不到頁面"`。
- **UI 呈現**：網頁儀表板保留卡片，壓暗彩度，加上 `🏚️ 已下架/已出租` 灰色標籤與專屬 Filter Pill，不破壞歷史評分紀錄。

### B. 雙重智慧去重

1. **Primary Key**：591 官方 `house_id`。
2. **地址結構化 + 價格坪數比對 (`is_precise_duplicate`)**

同一間房被不同仲介刊登時，地址粒度往往不同 —— 例如「敦化南路一段177巷」與「177號」、
「內湖路一段49號」與「內湖路一段」。因此本系統**不把地址當成認定重複的依據，只當否決條件**：

- `parse_address()` 把地址拆成 `行政區 / 路 / 段 / 巷 / 弄 / 號`，
  支援中文數字段（`三段`）、阿拉伯數字段（`3段`）與全形數字。
- `address_verdict()` 回傳三種結果：
  | 結果 | 條件 | 意義 |
  | :--- | :--- | :--- |
  | `conflict` | 區、路或段不同，或巷/弄/號兩邊都標示卻不同 | 確定不是同一間，直接否決 |
  | `compatible` | 已知欄位都吻合，只是一邊寫得比較細 | 可能是同一間 |
  | `unknown` | 至少一邊粒度太粗（例如只有「台北市中山區」） | 地址無法提供資訊 |
- 認定重複有兩條路徑：
  - **路徑 1（地址相容）**：`compatible` 且價差 ≤ 500 元、坪差 ≤ 0.5 坪。
  - **路徑 2（標題相似）**：價差 ≤ 1500、坪差 ≤ 1.5，且 SequenceMatcher 相似度
    仲介貼文 > 0.60 / 一般物件 > 0.70。

> ⚠️ 命中重複的房源會被直接丟棄不入庫，**誤判比漏抓更難察覺**。
> 調整任何門檻前請先跑 `python tests/test_dedup.py`，
> 該檔的正例與反例全部取自實際資料（含「同棟大樓不同戶」「同路不同段」等必須排除的情境）。

---

## 📂 6. 專案檔案結構說明 (Project Directory Structure)

```
OurHome/
├── app.py                      # Flask 雲端 Web 儀表板入口與背景獨立子程序爬蟲迴圈
├── dashboard.py                # 本地 HTTP 儀表板與 API Handler
├── ui_shared.py                # ⭐ app.py / dashboard.py 共用的前端 HTML_TEMPLATE 與資料格式化快取
├── db.py                       # SQLite 資料庫操作、WAL 模式、MD5 雜湊比較與 GitHub REST API 自動同步
├── scraper.py                  # 二階段 Playwright + requests 輕量化雙層爬蟲
├── cost_calculator.py          # 租屋真實月總成本計算器 (管理費、水電雜費解析)
├── notifier.py                 # Discord Webhook 嵌入式卡片發送器 (含來源標籤)
├── config.py                   # 全局配置檔 (.env 載入、多網址解析、User-Agents)
├── search_filters.py           # 591 目標搜尋網址與條件設定
├── main.py                     # 本地排程器入口 (Schedule + 儀表板背景執行緒)
├── run_crawler_standalone.py  # 獨立子程序爬蟲啟動器 (隔離 Web GIL 鎖定)
├── Procfile                    # Render 雲端生產環境 Gunicorn 啟動指令
├── requirements.txt            # Python 依賴套件套件清單
├── .env                        # 環境變數設定檔 (非公開)
├── .gitignore                  # Git 排除清單 (rentals.db 與 WAL 附屬檔；rentals_backup.json 刻意「不」排除)
└── rentals_backup.json         # 雲端 GitHub 雙向同步之 JSON 全量資料庫備份 (動態)
```

---

## 🗄️ 7. 資料庫 Schema (SQLite `rentals.db`)

```sql
CREATE TABLE IF NOT EXISTS houses (
    house_id TEXT PRIMARY KEY,               -- 591 物件 ID
    title TEXT NOT NULL,                     -- 房屋標題
    price TEXT,                              -- 原始刊登租金字串 (例如 "23,000元/月")
    numeric_price INTEGER,                   -- 刊登租金純數字 (例如 23000)
    address TEXT,                            -- 乾淨門牌/路段地址
    size TEXT,                               -- 坪數資訊 (例如 "10.5坪")
    link TEXT,                               -- 591 官方詳細頁連結
    address_fingerprint TEXT,                -- 地址+坪數特徵碼 (用於跨刊登去重)
    price_history TEXT,                      -- 歷史價格異動 JSON Array
    details_text TEXT,                       -- 費用細項與說明文字
    user_rating TEXT DEFAULT 'none',         -- 使用者評分 ('like'|'neutral'|'dislike'|'none')
    status TEXT DEFAULT 'active',            -- 上架狀態 ('active'|'off_market')
    missing_count INTEGER DEFAULT 0,         -- 消失計數器
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, -- 首次抓取時間
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP  -- 最後更新時間
);
```

---

## ⚙️ 8. 環境變數設定 (Environment Variables)

複製 [.env.example](file:///c:/personl/OurHome/.env.example) 成 `.env` 再填值。

> 🔐 **`.env` 已排除於版控之外，機密不可進 repo。**
> 正式環境請一律在 **Render Dashboard → Environment** 設定。
> `load_dotenv()` 預設 `override=False`，因此**平台環境變數的優先權高於 `.env` 檔案**——
> 雲端上 `.env` 的內容其實完全不會被採用。

| 環境變數 | 說明 | 範例值 |
| :--- | :--- | :--- |
| `MODE` | 租屋模式 (`couple` 雙人 400度 / `single` 單人 200度) | `couple` |
| `DISCORD_WEBHOOK_URL` | Discord 頻道 Webhook URL | `https://discord.com/api/webhooks/...` |
| `GITHUB_TOKEN` | GitHub Personal Access Token (用於雲端寫回 API) | `ghp_xxxxxxxxxxxxxxxxxxxx` |
| `TARGET_URL` | 591 搜尋網址 (有多條時可用逗號分隔) | `https://rent.591.com.tw/list?...` |
| `TARGET_URL_1`~`50` | 分號編號搜尋網址 | `https://rent.591.com.tw/list?...` |
| `CHECK_INTERVAL_MINUTES` | 巡邏間隔分鐘數 | `10` |
| `DB_PATH` | SQLite 資料庫檔名 | `rentals.db` |
| `RENDER` | Render 系統預設環境變數 (自動辨識來源) | `true` |

---

## 🚀 9. 部署與本地執行指南 (Deployment & Run Guide)

### 雲端部署 (Render Cloud Web Service)
1. 於 Render 新建 **Web Service** 並連接本 GitHub 儲存庫。
2. Build Command: `pip install -r requirements.txt && playwright install chromium`
3. Start Command: `gunicorn app:app --workers 1 --worker-class gthread --threads 4 --timeout 300 --keep-alive 5`
4. 環境變數：新增 `GITHUB_TOKEN` 與 `DISCORD_WEBHOOK_URL`。

### 本地執行 (Windows PC)
1. 安裝依賴：`pip install -r requirements.txt`
2. 安裝 Playwright：`playwright install chromium`
3. 啟動本機監控與儀表板：`python main.py`
4. 瀏覽器開啟：`http://localhost:5000`

---

## 🛠️ 10. 給 AI 開發者 (Claude / GPT) 的開發接手提示

1. **如需修改前端儀表板**：只要改 [ui_shared.py](file:///c:/personl/OurHome/ui_shared.py) 的 `HTML_TEMPLATE`，`app.py` 與 `dashboard.py` 會同時生效（兩邊只差各自的 `PAGE_TITLE` 常數）。卡片上的費用與標籤欄位則來自同檔的 `get_formatted_houses()`。
2. **如需修改去重與評分邏輯**：請參閱 [db.py](file:///c:/personl/OurHome/db.py) 中的 `update_house_rating` 與 `is_precise_duplicate`。請切記保持 `sync_backup_json()` 中的 MD5 Hash 比較邏輯，否則會引發 GitHub API 頻繁寫入。
3. **請勿把 `rentals_backup.json` 從 git 移除追蹤**：它是雲端 Master DB 的實體，untrack 並 commit 會直接把它從 GitHub repo 刪掉，導致所有節點還原失敗。詳見第 3-B 節。
4. **發送 Discord 通知前務必先填 `cost_info`**：`notifier.send_house_card()` 只負責排版，費用是由呼叫端用 `parse_rental_costs()` 算好塞進 `house["cost_info"]`。`run_crawler_standalone.py`（雲端路徑）與 `main.py`（本地路徑）都必須做這一步，漏掉卡片就會顯示「未估算」。
