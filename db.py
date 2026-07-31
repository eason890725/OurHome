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

GITHUB_REPO = "eason890725/OurHome"
GITHUB_FILE_PATH = "rentals_backup.json"
_LAST_PUSHED_HASH = ""

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
                
            conn.commit()
            
        self.restore_from_backup_json()

    def sync_backup_json(self, force_push: bool = False):
        """僅在 DB 內容實質變更 (Hash 改變) 時，才寫入硬碟並呼叫 GitHub API。

        兩道資料安全防線：
        1. 未成功讀到雲端 Master 前禁止回推（先嘗試補讀一次，補讀成功才放行）。
        2. `_LAST_PUSHED_HASH` 只在「確定推送成功」後才更新，
           否則推送失敗會被永久記成已推送，直到 DB 內容再次變動才重試。
        """
        global _LAST_PUSHED_HASH
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
            current_hash = hashlib.md5(json_bytes).hexdigest()

            # 若 Hash 沒有變更且非強制，直接跳過 (完全不寫硬碟也不呼叫 API)
            if not force_push and current_hash == _LAST_PUSHED_HASH:
                return

            tmp_file = "rentals_backup.json.tmp"
            with open(tmp_file, "wb") as f:
                f.write(json_bytes)
            os.replace(tmp_file, "rentals_backup.json")

            token = os.environ.get("GITHUB_TOKEN")
            if not token:
                # 本地無 token，不會推 GitHub；標記 hash 只是避免重複寫同樣的檔案
                _LAST_PUSHED_HASH = current_hash
                return

            # 防線 2：推成功才記錄 hash，失敗就維持舊值讓下次自動重試
            if self._push_to_github_api(token, json_bytes):
                _LAST_PUSHED_HASH = current_hash
            else:
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
                    "message": f"data: auto-sync rentals_backup.json ({item_count} items) from Render cloud [skip ci]",
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

                        cursor.execute("""
                            INSERT INTO houses (house_id, title, price, numeric_price, address, size, link, address_fingerprint, price_history, details_text, user_rating, status, missing_count, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                            h.get("created_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                            h.get("updated_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                        ))
                        restored_count += 1
                conn.commit()
                if restored_count > 0:
                    logger.info(f"✨ 成功從 GitHub rentals_backup.json 自動還原 {restored_count} 筆歷史房屋與評價紀錄！")
        except Exception as e:
            logger.error(f"從 rentals_backup.json 還原失敗: {e}")

    def update_house_rating(self, house_id: str, rating: str, sync_git: bool = True) -> bool:
        """更新房屋的使用者評價標記 (like / neutral / dislike / none)"""
        valid_ratings = {"like", "neutral", "dislike", "none"}
        if rating not in valid_ratings:
            rating = "none"
        
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE houses 
                SET user_rating = ?, updated_at = ?
                WHERE house_id = ?
            """, (rating, now_str, str(house_id)))
            conn.commit()
            success = cursor.rowcount > 0
            if success and sync_git:
                self.sync_backup_json()
            return success

    def is_precise_duplicate(self, new_house: dict, old_house: dict) -> bool:
        t1, t2 = new_house.get("title", ""), old_house.get("title", "")
        p1, p2 = new_house.get("numeric_price", 0), old_house.get("numeric_price", 0)
        s1_str, s2_str = new_house.get("size", ""), old_house.get("size", "")
        addr1 = (new_house.get("address") or "").replace(" ", "").replace("-", "")
        addr2 = (old_house.get("address") or "").replace(" ", "").replace("-", "")

        s1 = parse_sqft(s1_str)
        s2 = parse_sqft(s2_str)
        
        if s1 > 0 and s2 > 0 and abs(s1 - s2) > 1.5:
            return False

        price_diff = abs(p1 - p2)
        if price_diff > 1500:
            return False

        if addr1 and addr2 and "未提供" not in addr1 and "依現場" not in addr1 and addr1 == addr2:
            if s1 > 0 and s2 > 0 and abs(s1 - s2) <= 0.5:
                logger.info(f"🔁 [相同地址坪數去重] 命中相同地址 ({addr1}) 坪數 ({s1}坪 vs {s2}坪) 重複物件")
                return True

        t1_clean = clean_title_tokens(t1)
        t2_clean = clean_title_tokens(t2)
        ratio = SequenceMatcher(None, t1_clean, t2_clean).ratio()

        agency_match = ("美樂" in t1 and "美樂" in t2) or ("寄居蟹" in t1 and "寄居蟹" in t2)
        if agency_match and ratio > 0.60:
            return True

        if ratio > 0.70:
            return True

        return False

    def is_fuzzy_duplicate_property(self, house_data: dict) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT house_id, title, numeric_price, size, address FROM houses ORDER BY created_at DESC LIMIT 200")
            rows = cursor.fetchall()
            
            for row in rows:
                old_h = dict(row)
                if self.is_precise_duplicate(house_data, old_h):
                    logger.info(f"🔁 [精準去重] 辨識出重複刊登物件: [{house_data.get('title')[:20]}] (與 ID: {row['house_id']} {row['title'][:20]} 重複)")
                    return True

        return False

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
            cursor.execute("SELECT house_id, price, numeric_price, price_history, user_rating FROM houses WHERE house_id = ?", (house_id,))
            row = cursor.fetchone()

            if not row:
                if self.is_fuzzy_duplicate_property(house_data):
                    return {"action": "IGNORE"}

                initial_history = json.dumps([
                    {"price": current_price_str, "numeric": current_numeric_price, "time": now_str}
                ], ensure_ascii=False)

                cursor.execute("""
                    INSERT INTO houses (house_id, title, price, numeric_price, address, size, link, address_fingerprint, price_history, details_text, user_rating, status, missing_count, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    now_str,
                    now_str
                ))
                conn.commit()
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
                    cursor.execute("UPDATE houses SET status = ?, missing_count = 0, updated_at = ? WHERE house_id = ?", (status, now_str, house_id))
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
    db = HousingDB("c:/personl/OurHome/rentals.db")
