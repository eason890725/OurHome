import json
import re
import os
import time
import hashlib
import sqlite3
import logging
import base64
import requests
from datetime import datetime
from difflib import SequenceMatcher
from typing import Optional, Dict, List, Tuple, Any

logger = logging.getLogger(__name__)

# 雲端 Master DB 所在的 GitHub repo。設為空字串即進入「純本機模式」：
# 完全不下載也不上傳，rentals_backup.json 只當本地備份用。
# 預設值維持原本的 repo，因此既有 Render 部署不需要任何調整。
GITHUB_REPO = os.getenv("GITHUB_REPO", "eason890725/OurHome")
GITHUB_FILE_PATH = os.getenv("GITHUB_FILE_PATH", "rentals_backup.json")
_LAST_PUSHED_HASH = ""
# 上一次回推失敗、還有內容沒送上去。為 True 時不可套用「與硬碟相同就跳過」的閘門，
# 因為本地檔案在推送前就已寫入，硬碟內容看起來會跟即將要推的一樣。
_PUSH_PENDING = False

def canonical_hash(houses: List[Dict[str, Any]]) -> str:
    """內容雜湊，對排序與格式免疫。

    不能直接對 json.dumps 的位元組取雜湊：`ORDER BY created_at DESC` 在時間戳相同時
    順序未定義，縮排寫法也可能不同，兩者都會讓「內容其實一樣」被誤判成有變更。
    這裡固定以 house_id 排序、鍵名排序後再取雜湊。
    """
    try:
        canon = json.dumps(
            sorted(houses, key=lambda h: str(h.get("house_id", ""))),
            ensure_ascii=False, sort_keys=True
        )
        return hashlib.md5(canon.encode("utf-8")).hexdigest()
    except Exception:
        return ""


def _disk_backup_hash() -> str:
    """硬碟上 rentals_backup.json 的內容雜湊。

    開機時那份是剛從 GitHub 下載的，因此可以用來判斷
    「即將要推的內容是不是跟 GitHub 上完全相同」，而且**跨程序重啟仍然有效**。
    """
    try:
        with open("rentals_backup.json", "r", encoding="utf-8") as f:
            return canonical_hash(json.loads(sanitize_text(f.read())))
    except Exception:
        return ""


def sanitize_text(text: str) -> str:
    if not text:
        return ""
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(text))

def parse_numeric_price(price_str: str) -> int:
    if not price_str:
        return 0
    clean_str = re.sub(r'[^\d]', '', str(price_str))
    return int(clean_str) if clean_str else 0

def parse_sqft(size_str: str) -> float:
    if not size_str:
        return 0.0
    match = re.search(r'(\d+(?:\.\d+)?)', str(size_str))
    return float(match.group(1)) if match else 0.0

def clean_title_tokens(title: str) -> str:
    clean = re.sub(r'[^\w\u4e00-\u9fa5]', '', title)
    clean = re.sub(r'(可租補|有陽台|有管理|採光佳|景觀房|優質|搶租|精選|溫馨|大套房|電梯|獨洗|代收)', '', clean)
    return clean

CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def normalize_addr_text(s: str) -> str:
    """全形轉半形、移除空白與各種分隔符，讓後續解析不受排版差異影響。"""
    if not s:
        return ""
    out = []
    for ch in str(s):
        code = ord(ch)
        if 0xFF10 <= code <= 0xFF19 or 0xFF21 <= code <= 0xFF5A:  # 全形數字/英文
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return re.sub(r'[\s\-–—─,，.。()（）]', '', "".join(out))


def parse_address(address: str) -> Optional[Dict[str, Any]]:
    """把地址拆成 city/district/road/section/lane/alley/number。

    解析不出「行政區 + 路名」就回傳 None，代表這筆地址粒度太粗（例如只有「台北市中山區」），
    不足以拿來比對，交由標題相似度處理。
    """
    a = normalize_addr_text(address)
    if not a or "未提供" in a or "依現場" in a:
        return None

    parsed: Dict[str, Any] = {}
    m = re.search(r'([一-龥]{2,3}[市縣])', a)
    if m:
        parsed["city"] = m.group(1)
        a = a.replace(m.group(1), "", 1)

    m = re.search(r'([一-龥]{1,3}[區鄉鎮])', a)
    if not m:
        return None
    parsed["district"] = m.group(1)
    rest = a[m.end():]

    m = re.search(r'^([一-龥A-Za-z0-9]+?[路街道])', rest)
    if not m:
        return None
    parsed["road"] = m.group(1)
    rest = rest[m.end():]

    # 段：同時支援「三段」與「3段」
    m = re.search(r'^([一二三四五六七八九十\d]+)段', rest)
    if m:
        tok = m.group(1)
        parsed["section"] = CN_NUM.get(tok) or (int(tok) if tok.isdigit() else None)
        rest = rest[m.end():]

    for key, pat in (("lane", r'(\d+)巷'), ("alley", r'(\d+)弄'), ("number", r'(\d+)號')):
        m = re.search(pat, rest)
        if m:
            parsed[key] = int(m.group(1))
    return parsed


def address_verdict(addr1: str, addr2: str) -> str:
    """比對兩個地址，回傳 conflict / compatible / unknown。

    設計原則：**地址只用來「排除」，不用來「認定」**。
    - conflict   ：區、路或段不同 → 確定不是同一間，直接否決
    - compatible ：已知欄位都吻合，只是一邊寫得比較細
                   （例如「內湖路一段」vs「內湖路一段49號」、「177巷」vs「177號」）
    - unknown    ：至少一邊粒度太粗解析不出來，地址無法提供資訊
    """
    p1, p2 = parse_address(addr1), parse_address(addr2)
    if p1 is None or p2 is None:
        return "unknown"
    if p1["district"] != p2["district"] or p1["road"] != p2["road"]:
        return "conflict"
    # 段：兩邊都標了才比。新生北路二段 vs 三段是不同地方。
    if p1.get("section") and p2.get("section") and p1["section"] != p2["section"]:
        return "conflict"
    # 巷/弄/號：兩邊都標了才比；只有一邊標代表另一邊寫得粗，視為相容
    for key in ("lane", "alley", "number"):
        if key in p1 and key in p2 and p1[key] != p2[key]:
            return "conflict"
    return "compatible"


def generate_address_fingerprint(address: str, size_str: str, price_str: str) -> str:
    if not address or address == "未提供地址" or "依現場" in address:
        return ""
    
    clean_addr = re.sub(r'(捷運|站|步|分|近|距|約|公尺|高樓層|獨戶|獨立|精緻|電梯|公寓|華廈|隨時|拎包|依現場|社區名稱)', '', address)
    clean_addr = re.sub(r'[\s\-–—─,，.。（）\(\)]', '', clean_addr)
    clean_size = parse_sqft(size_str)
    return f"{clean_addr}_{clean_size}坪" if clean_addr else ""

class HousingDB:
    def __init__(self, db_path: str = "rentals.db"):
        self.db_path = db_path
        # 是否成功讀到雲端 Master 備份。False 時一律禁止回推，
        # 避免「開機讀取失敗 → DB 是空的 → 把空資料推上去蓋掉正確版本」。
        # 必須在 _init_db() 之前設定，因為 _init_db() 結尾會呼叫 restore_from_backup_json()。
        self._master_loaded = False
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """建立具備 WAL 高併發模式與 30 秒解鎖緩衝的 SQLite 連線"""
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
        except Exception:
            pass
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS houses (
                    house_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    price TEXT,
                    numeric_price INTEGER,
                    address TEXT,
                    size TEXT,
                    link TEXT,
                    address_fingerprint TEXT,
                    price_history TEXT,
                    details_text TEXT,
                    user_rating TEXT DEFAULT 'none',
                    status TEXT DEFAULT 'active',
                    missing_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            cursor.execute("PRAGMA table_info(houses)")
            columns = [column[1] for column in cursor.fetchall()]
            if "address_fingerprint" not in columns:
                cursor.execute("ALTER TABLE houses ADD COLUMN address_fingerprint TEXT")
            if "price_history" not in columns:
                cursor.execute("ALTER TABLE houses ADD COLUMN price_history TEXT")
            if "numeric_price" not in columns:
                cursor.execute("ALTER TABLE houses ADD COLUMN numeric_price INTEGER")
            if "details_text" not in columns:
                cursor.execute("ALTER TABLE houses ADD COLUMN details_text TEXT")
            if "user_rating" not in columns:
                cursor.execute("ALTER TABLE houses ADD COLUMN user_rating TEXT DEFAULT 'none'")
            if "status" not in columns:
                cursor.execute("ALTER TABLE houses ADD COLUMN status TEXT DEFAULT 'active'")
            if "missing_count" not in columns:
                cursor.execute("ALTER TABLE houses ADD COLUMN missing_count INTEGER DEFAULT 0")
            if "updated_at" not in columns:
                cursor.execute("ALTER TABLE houses ADD COLUMN updated_at TIMESTAMP")
            # 重複刊登指向的主物件 house_id。NULL 代表它自己就是主物件。
            # 刻意「標記」而非「丟棄」：誤判時資料還在，儀表板展開就看得到。
            if "duplicate_of" not in columns:
                cursor.execute("ALTER TABLE houses ADD COLUMN duplicate_of TEXT")
            # 命中排除關鍵字時記下是哪一個。NULL = 沒被排除。
            # 記錄關鍵字而不是直接刪除，使用者調整清單後才能回溯生效，也才知道為什麼被濾掉。
            if "excluded_by" not in columns:
                cursor.execute("ALTER TABLE houses ADD COLUMN excluded_by TEXT")

            conn.commit()

        self.restore_from_backup_json()

        # 還原之後立刻重建去重與關鍵字排除的標記。
        # 不能只依賴巡邏結束時才做——巡邏一旦被中斷（例如容器重啟）就永遠補不回來。
        # 實際發生過：服務每 2.5 分鐘重啟，去重標記全部歸零、儀表板又出現大量重複。
        try:
            from config import EXCLUDE_KEYWORDS as _EXCLUDE
            self.apply_exclude_keywords(_EXCLUDE)
            self.dedupe_existing()
        except Exception as e:
            logger.error(f"啟動時重建去重／排除標記失敗（不影響服務）: {e}")

    def sync_backup_json(self, force_push: bool = False):
        """僅在 DB 內容實質變更 (Hash 改變) 時，才寫入硬碟並呼叫 GitHub API。

        兩道資料安全防線：
        1. 未成功讀到雲端 Master 前禁止回推（先嘗試補讀一次，補讀成功才放行）。
        2. `_LAST_PUSHED_HASH` 只在「確定推送成功」後才更新，
           否則推送失敗會被永久記成已推送，直到 DB 內容再次變動才重試。
        """
        global _LAST_PUSHED_HASH, _PUSH_PENDING
        houses = self.get_all_houses()
        if not houses:
            return

        # 防線 1：開機時沒讀到雲端主檔就不准推。先補讀一次，成功了才繼續。
        if not self._master_loaded:
            logger.warning("⚠️ 尚未成功讀取雲端 Master 備份，先嘗試補讀後再決定是否回推...")
            self.restore_from_backup_json()
            if not self._master_loaded:
                logger.error("🛑 仍讀不到雲端 Master 備份，本次「不」回推，避免以不完整資料覆蓋雲端。")
                return
            houses = self.get_all_houses()  # 補讀可能還原了資料，重新取得

        try:
            json_bytes = json.dumps(houses, ensure_ascii=False, indent=2).encode("utf-8")
            current_hash = canonical_hash(houses)

            # 閘門一：本程序這輪已經推過同樣內容
            if not force_push and current_hash == _LAST_PUSHED_HASH:
                return

            # 閘門二：跟硬碟上那份（開機時剛從 GitHub 下載）比對。
            # _LAST_PUSHED_HASH 是程序內變數，重啟後歸零，光靠它擋不住
            # 「重啟 → 還原 → 內容其實一模一樣卻照推」的情況；
            # 服務每 2.5 分鐘重啟一次時，這會變成每 2.5 分鐘一個 GitHub commit。
            # 有待推內容時不能套用，否則上次失敗的推送會被誤判成已完成。
            if not force_push and not _PUSH_PENDING and current_hash == _disk_backup_hash():
                _LAST_PUSHED_HASH = current_hash
                return

            # 暫存檔名帶上 PID：Web 程序與爬蟲子程序可能同時寫入，
            # 共用檔名會讓其中一個 os.replace 找不到檔案而拋錯。
            tmp_file = f"rentals_backup.json.{os.getpid()}.tmp"
            with open(tmp_file, "wb") as f:
                f.write(json_bytes)
            os.replace(tmp_file, "rentals_backup.json")

            token = os.environ.get("GITHUB_TOKEN")
            if not token or not GITHUB_REPO:
                # 純本機模式或未設 token：不會推 GitHub。
                # 標記 hash 只是避免下次重複寫出同樣的檔案。
                _LAST_PUSHED_HASH = current_hash
                return

            # 防線 2：推成功才記錄 hash，失敗就維持舊值讓下次自動重試
            if self._push_to_github_api(token, json_bytes):
                _LAST_PUSHED_HASH = current_hash
                _PUSH_PENDING = False
            else:
                _PUSH_PENDING = True
                logger.warning("⚠️ 本次 GitHub 回推未成功，保留舊 hash，下次同步會自動重試。")

        except Exception as e:
            logger.error(f"同步 rentals_backup.json 失敗: {e}")

    def _push_to_github_api(self, token: str, json_bytes: bytes) -> bool:
        """透由 GitHub REST API 自動 Commit 並 Push 最新備份至 GitHub 儲存庫。

        回傳 True 代表確定寫入成功 (HTTP 200/201)。
        遇到 409/422 (sha 過期，代表推送期間檔案被別人改過) 會重抓 sha 再試一次。
        """
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json"
        }
        item_count = len(json.loads(json_bytes.decode("utf-8")))
        content_b64 = base64.b64encode(json_bytes).decode("utf-8")

        # ⚠️ commit 訊息必須含 [skip render]，否則會觸發 Render 重新部署，
        #    形成「巡邏 → 寫回 GitHub → 重新部署 → 90 秒後又巡邏」的無窮迴圈。
        #    實際發生過：部署事件裡出現 "Deploy live for <sha>: data: auto-sync ..."，
        #    連帶讓 Chromium 啟動頻率遠高於設定值而撐爆 512MB 記憶體。
        #    [skip ci] 只對 GitHub Actions 有效，Render 不認得。
        commit_message = (
            f"data: auto-sync rentals_backup.json ({item_count} items) "
            f"from Render cloud [skip render] [skip ci]"
        )

        def _current_sha() -> str:
            try:
                get_resp = requests.get(url, headers=headers, timeout=5)
                if get_resp.status_code == 200:
                    return get_resp.json().get("sha", "")
            except Exception as e:
                logger.warning(f"取得 GitHub 現行 sha 失敗: {e}")
            return ""

        for attempt in (1, 2):
            try:
                payload = {
                    "message": commit_message,
                    "content": content_b64,
                    "branch": "main"
                }
                sha = _current_sha()
                if sha:
                    payload["sha"] = sha

                put_resp = requests.put(url, headers=headers, json=payload, timeout=8)
                if put_resp.status_code in (200, 201):
                    logger.info(f"✨ [GitHub API 雙向全自動同步成功] 已把 Render 最新 {item_count} 筆 DB 寫回 GitHub！")
                    return True

                if put_resp.status_code in (409, 422) and attempt == 1:
                    logger.warning(f"GitHub 回推遇到 sha 衝突 (HTTP {put_resp.status_code})，重抓 sha 後重試...")
                    continue

                logger.error(f"❌ GitHub API 回推失敗: HTTP {put_resp.status_code} - {put_resp.text[:300]}")
                return False

            except Exception as e:
                logger.error(f"❌ GitHub API 回推異常 (第 {attempt} 次): {e}")
                if attempt == 1:
                    continue
                return False

        return False

    def restore_from_backup_json(self):
        """優先從 GitHub REST API / Raw 下載最新雲端 rentals_backup.json 並自動還原至 SQLite 資料庫。

        成功讀到「可解析且非空」的主檔時才會把 self._master_loaded 設為 True；
        沒設起來的話 sync_backup_json() 會拒絕回推，防止空資料覆蓋雲端。
        """
        downloaded = False

        if not GITHUB_REPO:
            # 純本機模式：沒有遠端 Master 需要保護，直接放行本地寫入
            self._master_loaded = True
            if not os.path.exists("rentals_backup.json"):
                logger.info("🏠 純本機模式（未設定 GITHUB_REPO），將建立全新的本地資料庫")
                return
            logger.info("🏠 純本機模式（未設定 GITHUB_REPO），僅使用本地 rentals_backup.json")
        else:
            try:
                raw_url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{GITHUB_FILE_PATH}"
                headers = {"User-Agent": "Mozilla/5.0"}
                token = os.environ.get("GITHUB_TOKEN")
                if token:
                    headers["Authorization"] = f"token {token}"

                resp = requests.get(raw_url, headers=headers, timeout=8)
                if resp.status_code == 200 and resp.text.strip():
                    # 先確認下載內容真的是合法 JSON，再覆蓋本地主檔，
                    # 避免收到半截或錯誤頁面時把好的本地備份寫壞。
                    json.loads(sanitize_text(resp.text))
                    with open("rentals_backup.json", "w", encoding="utf-8") as f:
                        f.write(resp.text)
                    downloaded = True
                else:
                    logger.warning(f"⚠️ 下載雲端 Master 備份未成功: HTTP {resp.status_code}")
            except Exception as e:
                logger.warning(f"⚠️ 下載雲端 Master 備份失敗，改用本地既有備份: {e}")

            if not os.path.exists("rentals_backup.json"):
                logger.error("🛑 雲端下載失敗且本地無 rentals_backup.json，無法確認 Master 內容。")
                return
        try:
            with open("rentals_backup.json", "r", encoding="utf-8") as f:
                raw_content = f.read()

            cleaned_content = sanitize_text(raw_content)
            houses = json.loads(cleaned_content)
            if not houses:
                logger.warning("⚠️ Master 備份可解析但內容為空，不視為讀取成功。")
                return

            # 到這裡代表確實拿到一份可用的 Master，放行回推
            self._master_loaded = True
            logger.info(f"📥 已載入 Master 備份 ({len(houses)} 筆，來源: {'GitHub 雲端' if downloaded else '本地既有檔案'})")

            with self._get_connection() as conn:
                cursor = conn.cursor()
                restored_count = 0
                for h in houses:
                    house_id = str(h.get("house_id", ""))
                    if not house_id:
                        continue
                    cursor.execute("SELECT house_id FROM houses WHERE house_id = ?", (house_id,))
                    if not cursor.fetchone():
                        history_val = h.get("price_history", "[]")
                        if isinstance(history_val, (list, dict)):
                            history_str = json.dumps(history_val, ensure_ascii=False)
                        else:
                            history_str = str(history_val)

                        # duplicate_of / excluded_by 一定要一起還原。
                        # 漏掉的話，每次重啟從備份還原就會把去重與關鍵字過濾的結果清空，
                        # 而重建這些標記要等下一輪巡邏跑完——巡邏若被中斷就永遠補不回來。
                        cursor.execute("""
                            INSERT INTO houses (house_id, title, price, numeric_price, address, size, link, address_fingerprint, price_history, details_text, user_rating, status, missing_count, duplicate_of, excluded_by, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            house_id,
                            sanitize_text(h.get("title", "")),
                            sanitize_text(h.get("price", "")),
                            h.get("numeric_price", 0),
                            sanitize_text(h.get("address", "")),
                            sanitize_text(h.get("size", "")),
                            sanitize_text(h.get("link", "")),
                            sanitize_text(h.get("address_fingerprint", "")),
                            history_str,
                            sanitize_text(h.get("details_text", "")),
                            h.get("user_rating", "none"),
                            h.get("status", "active"),
                            h.get("missing_count", 0),
                            h.get("duplicate_of") or None,
                            h.get("excluded_by") or None,
                            h.get("created_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                            h.get("updated_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                        ))
                        restored_count += 1
                conn.commit()
                if restored_count > 0:
                    logger.info(f"✨ 成功從 GitHub rentals_backup.json 自動還原 {restored_count} 筆歷史房屋與評價紀錄！")
        except Exception as e:
            logger.error(f"從 rentals_backup.json 還原失敗: {e}")

    def set_house_rating(self, house_id: str, rating: str) -> Tuple[bool, bool]:
        """寫入使用者評價，回傳 (是否找到該物件, 是否真的有變更)。本身不做任何同步。

        **值沒變就完全不寫入**。這點很重要：儀表板每次開啟都會把 localStorage 裡的
        評分整包 POST 回來，若照舊無條件改寫 updated_at，備份 JSON 的內容就會跟著變，
        sync_backup_json() 的 MD5 閘門形同虛設，於是每開一次頁面就產生一個 GitHub commit。
        """
        valid_ratings = {"like", "neutral", "dislike", "none"}
        if rating not in valid_ratings:
            rating = "none"

        hid = str(house_id)
        with self._get_connection() as conn:
            cursor = conn.cursor()
            row = cursor.execute("SELECT user_rating FROM houses WHERE house_id = ?", (hid,)).fetchone()
            if not row:
                return False, False
            if (row["user_rating"] or "none") == rating:
                return True, False

            cursor.execute(
                "UPDATE houses SET user_rating = ?, updated_at = ? WHERE house_id = ?",
                (rating, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), hid)
            )
            conn.commit()
            return True, True

    def update_house_rating(self, house_id: str, rating: str, sync_git: bool = True) -> bool:
        """更新房屋的使用者評價標記 (like / neutral / dislike / none)。回傳是否找到該物件。"""
        found, changed = self.set_house_rating(house_id, rating)
        if changed and sync_git:
            self.sync_backup_json()
        return found

    # 「地址相容」路徑的門檻，依價格接近程度分兩級。
    #
    # 同一間房被不同仲介刊登時，坪數常常對不起來——有人寫權狀坪、有人寫室內坪，
    # 實際觀察到同一間房出現 8.2 坪與 9 坪兩種寫法。因此當租金幾乎完全一致時
    # （價差 <= 200 元，這種巧合在不同物件上很罕見），坪數容忍度放寬到 1.5 坪。
    # 租金只是接近而非幾乎相同時，仍維持 0.5 坪的嚴格門檻。
    SAME_PRICE_TOLERANCE = 200
    SAME_PRICE_SIZE_TOLERANCE = 1.5
    COMPAT_PRICE_TOLERANCE = 500
    COMPAT_SIZE_TOLERANCE = 0.5

    def is_precise_duplicate(self, new_house: dict, old_house: dict) -> bool:
        """判斷兩筆刊登是否為同一間房屋。

        地址在這裡只扮演「否決者」：解析出行政區/路/段之後，只要任一層級衝突就直接排除
        （例如新生北路二段 vs 三段）。認定重複則交給價格與坪數——實測顯示這兩者才有鑑別力，
        因為不同仲介的標題充滿 emoji 與行銷詞，相似度撐不起來。
        """
        t1, t2 = new_house.get("title", ""), old_house.get("title", "")
        p1 = new_house.get("numeric_price", 0) or 0
        p2 = old_house.get("numeric_price", 0) or 0
        addr1 = new_house.get("address") or ""
        addr2 = old_house.get("address") or ""

        s1 = parse_sqft(new_house.get("size", ""))
        s2 = parse_sqft(old_house.get("size", ""))
        size_diff = abs(s1 - s2) if (s1 > 0 and s2 > 0) else None
        price_diff = abs(p1 - p2)

        verdict = address_verdict(addr1, addr2)
        if verdict == "conflict":
            return False

        # 路徑 1：地址相容（一邊寫到門牌、一邊只寫到路段也算）+ 價格與坪數相符
        if verdict == "compatible" and size_diff is not None:
            same_price = (price_diff <= self.SAME_PRICE_TOLERANCE
                          and size_diff <= self.SAME_PRICE_SIZE_TOLERANCE)
            close_price = (price_diff <= self.COMPAT_PRICE_TOLERANCE
                           and size_diff <= self.COMPAT_SIZE_TOLERANCE)
            if same_price or close_price:
                logger.info(
                    f"🔁 [地址相容去重] {addr1!r} ≈ {addr2!r}｜價差 {price_diff} 元｜坪差 {size_diff:.1f}"
                )
                return True

        # 路徑 2：標題高度相似（沿用原有門檻；地址衝突者已在上面被擋掉）
        if size_diff is not None and size_diff > 1.5:
            return False
        if price_diff > 1500:
            return False

        ratio = SequenceMatcher(None, clean_title_tokens(t1), clean_title_tokens(t2)).ratio()
        agency_match = ("美樂" in t1 and "美樂" in t2) or ("寄居蟹" in t1 and "寄居蟹" in t2)
        if (agency_match and ratio > 0.60) or ratio > 0.70:
            logger.info(f"🔁 [標題相似去重] 相似度 {ratio:.2f}｜{t1[:20]!r} ≈ {t2[:20]!r}")
            return True

        return False

    # 去重比對的回溯上限。原本寫死 200，資料量一超過就會無聲地漏掉較舊的重複刊登。
    # is_precise_duplicate() 會先做便宜的地址否決，絕大多數配對在跑到 SequenceMatcher
    # 之前就被排除，因此這個數字可以放大。
    DEDUP_SCAN_LIMIT = 2000

    def find_duplicate_of(self, house_data: dict) -> Optional[str]:
        """找出這筆房源是哪個既有物件的重複刊登，回傳主物件的 house_id。

        只比對「本身不是重複刊登」的物件，讓重複群always指向同一個主物件，
        不會形成 A→B→C 的鏈。
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT house_id, title, numeric_price, size, address FROM houses "
                "WHERE duplicate_of IS NULL AND house_id != ? "
                "ORDER BY created_at ASC LIMIT ?",
                (str(house_data.get("house_id", "")), self.DEDUP_SCAN_LIMIT)
            )
            rows = cursor.fetchall()

            if len(rows) == self.DEDUP_SCAN_LIMIT:
                logger.warning(
                    f"⚠️ 去重比對已達回溯上限 {self.DEDUP_SCAN_LIMIT} 筆，"
                    f"更舊的物件不會被比對到，請考慮調高 DEDUP_SCAN_LIMIT。"
                )

            for row in rows:
                if self.is_precise_duplicate(house_data, dict(row)):
                    return str(row["house_id"])
        return None

    def apply_exclude_keywords(self, keywords: List[str]) -> Tuple[int, int]:
        """依目前的排除關鍵字清單，回頭重新檢查所有既有房源。

        回傳 (新標記為排除的筆數, 解除排除的筆數)。

        過濾原本只在物件第一次被抓到時執行，因此使用者在 .env 加了新關鍵字之後，
        既有的房源不會有任何變化。這個方法讓清單調整能回溯生效；
        反過來把關鍵字拿掉時，先前被排除的也會自動放回來。
        """
        marked = unmarked = 0
        with self._get_connection() as conn:
            cursor = conn.cursor()
            rows = cursor.execute(
                "SELECT house_id, title, details_text, address, excluded_by FROM houses"
            ).fetchall()

            for r in rows:
                text = f"{r['title'] or ''} {r['address'] or ''} {r['details_text'] or ''}"
                hit = next((kw for kw in keywords if kw and kw in text), None)
                current = r["excluded_by"]
                if hit == current:
                    continue
                cursor.execute("UPDATE houses SET excluded_by = ? WHERE house_id = ?",
                               (hit, r["house_id"]))
                if hit:
                    marked += 1
                    logger.info(f"🚫 [關鍵字排除] [{r['house_id']}] {(r['title'] or '')[:20]} ← 命中「{hit}」")
                else:
                    unmarked += 1
                    logger.info(f"↩️ [關鍵字排除] 解除 [{r['house_id']}] {(r['title'] or '')[:20]}")
            conn.commit()

        if marked or unmarked:
            logger.info(f"🚫 關鍵字過濾更新：新排除 {marked} 筆、解除 {unmarked} 筆")
        return marked, unmarked

    def dedupe_existing(self) -> int:
        """回頭掃描既有資料，把重複刊登標記到主物件上，回傳新標記的筆數。

        去重原本只在物件「第一次被抓到」時才執行，因此在演算法調整之前就已經
        進到資料庫的重複刊登永遠不會被處理。每輪巡邏跑一次這個，讓既有資料也能收斂。

        以最早建立的為主物件（created_at 較早者保留），避免主從關係隨掃描順序跳動。
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            rows = [dict(r) for r in cursor.execute(
                "SELECT house_id, title, numeric_price, size, address, duplicate_of "
                "FROM houses ORDER BY created_at ASC LIMIT ?", (self.DEDUP_SCAN_LIMIT,)
            ).fetchall()]

            primaries: List[Dict[str, Any]] = []
            marked = 0
            for h in rows:
                hit = None
                for p in primaries:
                    if self.is_precise_duplicate(h, p):
                        hit = p
                        break
                if hit is None:
                    primaries.append(h)
                    # 原本被標記成重複、但重新判定後已不是的，要解除標記
                    if h.get("duplicate_of"):
                        cursor.execute("UPDATE houses SET duplicate_of = NULL WHERE house_id = ?",
                                       (h["house_id"],))
                        logger.info(f"↩️ [去重] 解除誤標: [{h['house_id']}] {(h.get('title') or '')[:20]}")
                    continue

                if h.get("duplicate_of") == hit["house_id"]:
                    continue           # 已經標好了
                cursor.execute("UPDATE houses SET duplicate_of = ? WHERE house_id = ?",
                               (hit["house_id"], h["house_id"]))
                marked += 1
                logger.info(
                    f"🔁 [去重] 標記重複刊登: [{h['house_id']}] {(h.get('title') or '')[:18]} "
                    f"→ 主物件 [{hit['house_id']}] {(hit.get('title') or '')[:18]}"
                )
            conn.commit()

        if marked:
            logger.info(f"🔁 本次共標記 {marked} 筆重複刊登")
        return marked

    def process_house(self, house_data: Dict[str, Any]) -> Dict[str, Any]:
        house_id = str(house_data.get("house_id", ""))
        if not house_id:
            return {"action": "IGNORE"}

        title = sanitize_text(house_data.get("title", ""))
        current_price_str = sanitize_text(house_data.get("price", "0"))
        current_numeric_price = parse_numeric_price(current_price_str)
        address = sanitize_text(house_data.get("address", ""))
        size = sanitize_text(house_data.get("size", ""))
        details_text = sanitize_text(house_data.get("details_text", ""))
        status = house_data.get("status", "active")
        fingerprint = generate_address_fingerprint(address, size, current_price_str)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        house_data["numeric_price"] = current_numeric_price
        house_data["address_fingerprint"] = fingerprint

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT house_id, price, numeric_price, price_history, user_rating, status, missing_count "
                "FROM houses WHERE house_id = ?", (house_id,)
            )
            row = cursor.fetchone()

            if not row:
                # 重複刊登照樣入庫，只是標記指向主物件。
                # 直接丟棄的話，一旦誤判就再也看不到那筆房源，而且無從察覺。
                dup_of = self.find_duplicate_of(house_data)
                if dup_of:
                    logger.info(
                        f"🔁 [去重] 新物件為重複刊登: [{house_id}] {title[:18]} "
                        f"→ 主物件 [{dup_of}]"
                    )

                initial_history = json.dumps([
                    {"price": current_price_str, "numeric": current_numeric_price, "time": now_str}
                ], ensure_ascii=False)

                cursor.execute("""
                    INSERT INTO houses (house_id, title, price, numeric_price, address, size, link, address_fingerprint, price_history, details_text, user_rating, status, missing_count, duplicate_of, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    house_id,
                    title,
                    current_price_str,
                    current_numeric_price,
                    address,
                    size,
                    house_data.get("link", ""),
                    fingerprint,
                    initial_history,
                    details_text,
                    'none',
                    status,
                    0,
                    dup_of,
                    now_str,
                    now_str
                ))
                conn.commit()

                if dup_of:
                    # 重複刊登不發 Discord 通知，否則同一間房會通知好幾次
                    return {"action": "DUPLICATE", "house": house_data}

                logger.info(f"記錄全新不重複房屋物件: [{house_id}] {title}")
                return {"action": "NEW", "house": house_data}

            else:
                old_numeric_price = row["numeric_price"] or parse_numeric_price(row["price"])
                old_price_str = row["price"] or str(old_numeric_price)

                try:
                    history = json.loads(row["price_history"]) if row["price_history"] else []
                except Exception:
                    history = []

                if current_numeric_price > 0 and old_numeric_price > 0 and current_numeric_price < old_numeric_price:
                    drop_amount = old_numeric_price - current_numeric_price
                    history.append({"price": current_price_str, "numeric": current_numeric_price, "time": now_str})

                    cursor.execute("""
                        UPDATE houses 
                        SET price = ?, numeric_price = ?, price_history = ?, details_text = ?, status = ?, missing_count = 0, updated_at = ?
                        WHERE house_id = ?
                    """, (
                        current_price_str,
                        current_numeric_price,
                        json.dumps(history, ensure_ascii=False),
                        details_text,
                        status,
                        now_str,
                        house_id
                    ))
                    conn.commit()

                    logger.info(f"🚨 檢測到房屋降價！[{house_id}] 原價: {old_price_str} -> 新價: {current_price_str} (直降 {drop_amount:,} 元)")
                    
                    updated_house = dict(house_data)
                    updated_house["old_price"] = old_price_str
                    updated_house["drop_amount"] = f"{drop_amount:,} 元"

                    return {
                        "action": "PRICE_DROP",
                        "old_price": old_price_str,
                        "new_price": current_price_str,
                        "drop_amount": f"{drop_amount:,} 元",
                        "house": updated_house
                    }
                else:
                    # 只有在狀態真的改變時才寫入。原本無條件改寫 updated_at，
                    # 導致每輪巡邏的備份 JSON 內容都不同、MD5 閘門永遠攔不下來，
                    # 每 10 分鐘就無謂地推一次 GitHub。
                    if (row["status"] or "active") != status or (row["missing_count"] or 0) != 0:
                        cursor.execute(
                            "UPDATE houses SET status = ?, missing_count = 0, updated_at = ? WHERE house_id = ?",
                            (status, now_str, house_id)
                        )
                        conn.commit()
                    return {"action": "IGNORE"}

    def process_houses_batch(self, houses_list: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        results = {
            "new_houses": [],
            "price_drop_houses": []
        }

        for house in houses_list:
            res = self.process_house(house)
            if res["action"] == "NEW":
                results["new_houses"].append(res["house"])
            elif res["action"] == "PRICE_DROP":
                results["price_drop_houses"].append(res["house"])
        
        # 回頭處理既有資料：演算法調整前就已入庫的重複刊登，
        # 不會因為「只在新物件時檢查」而永遠留在列表上。
        try:
            self.dedupe_existing()
        except Exception as e:
            logger.error(f"既有資料去重失敗（不影響巡邏）: {e}")

        self.sync_backup_json()
        self.checkpoint_wal()
        return results

    def checkpoint_wal(self):
        """把 WAL 內容併回主資料庫並截斷 WAL 檔。

        WAL 模式下 SQLite 只會重複使用 -wal 檔的空間、不會自動縮小它，
        長期執行下來 rentals.db-wal 會停在歷史高水位 (曾觀察到 4.8MB)。
        TRUNCATE 模式的 checkpoint 會把它歸零。若當下有其他連線正在讀取
        會回傳 busy，此時安靜跳過即可，下一輪巡邏再試。
        """
        try:
            with self._get_connection() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except Exception as e:
            logger.debug(f"WAL checkpoint 跳過 (可能有其他連線佔用): {e}")

    def get_all_houses(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM houses ORDER BY created_at DESC")
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    db = HousingDB(os.getenv("DB_PATH", "rentals.db"))
    print(f"目前資料庫共 {len(db.get_all_houses())} 筆房屋紀錄")
