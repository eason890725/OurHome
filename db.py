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
        """僅在 DB 內容實質變更 (Hash 改變) 時，才寫入硬碟並呼叫 GitHub API"""
        global _LAST_PUSHED_HASH
        houses = self.get_all_houses()
        if not houses:
            return
        try:
            json_bytes = json.dumps(houses, ensure_ascii=False, indent=2).encode("utf-8")
            current_hash = hashlib.md5(json_bytes).hexdigest()
            
            # 若 Hash 沒有變更且非強制，直接跳過 (完全不寫硬碟也不呼叫 API)
            if not force_push and current_hash == _LAST_PUSHED_HASH:
                return

            _LAST_PUSHED_HASH = current_hash

            tmp_file = "rentals_backup.json.tmp"
            with open(tmp_file, "wb") as f:
                f.write(json_bytes)
            os.replace(tmp_file, "rentals_backup.json")
            
            token = os.environ.get("GITHUB_TOKEN")
            if token:
                self._push_to_github_api(token, json_bytes)

        except Exception as e:
            logger.debug(f"同步 rentals_backup.json 失敗: {e}")

    def _push_to_github_api(self, token: str, json_bytes: bytes):
        """透由 GitHub REST API 自動 Commit 並 Push 最新備份至 GitHub 儲存庫"""
        try:
            url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"
            headers = {
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json"
            }
            
            sha = ""
            get_resp = requests.get(url, headers=headers, timeout=5)
            if get_resp.status_code == 200:
                sha = get_resp.json().get("sha", "")

            content_b64 = base64.b64encode(json_bytes).decode("utf-8")
            payload = {
                "message": f"data: auto-sync rentals_backup.json ({len(json.loads(json_bytes.decode('utf-8')))} items) from Render cloud [skip ci]",
                "content": content_b64,
                "branch": "main"
            }
            if sha:
                payload["sha"] = sha

            put_resp = requests.put(url, headers=headers, json=payload, timeout=8)
            if put_resp.status_code in (200, 201):
                logger.info(f"✨ [GitHub API 雙向全自動同步成功] 已把 Render 最新 {len(json.loads(json_bytes.decode('utf-8')))} 筆 DB 寫回 GitHub！")
            else:
                logger.debug(f"GitHub API 同步提示: {put_resp.status_code} - {put_resp.text}")

        except Exception as e:
            logger.debug(f"GitHub API 雙向同步異常: {e}")

    def restore_from_backup_json(self):
        """優先從 GitHub REST API / Raw 下載最新雲端 rentals_backup.json 並自動還原至 SQLite 資料庫"""
        try:
            raw_url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{GITHUB_FILE_PATH}"
            headers = {"User-Agent": "Mozilla/5.0"}
            token = os.environ.get("GITHUB_TOKEN")
            if token:
                headers["Authorization"] = f"token {token}"
            
            resp = requests.get(raw_url, headers=headers, timeout=8)
            if resp.status_code == 200 and resp.text.strip():
                with open("rentals_backup.json", "w", encoding="utf-8") as f:
                    f.write(resp.text)
        except Exception as e:
            logger.debug(f"下載雲端 rentals_backup.json 提示: {e}")

        if not os.path.exists("rentals_backup.json"):
            return
        try:
            with open("rentals_backup.json", "r", encoding="utf-8") as f:
                raw_content = f.read()
            
            cleaned_content = sanitize_text(raw_content)
            houses = json.loads(cleaned_content)
            if not houses:
                return
            
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
        return results

    def get_all_houses(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM houses ORDER BY created_at DESC")
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    db = HousingDB("c:/personl/OurHome/rentals.db")
