"""591 租屋網爬蟲——純 HTTP，不使用瀏覽器。

591 的搜尋結果頁是伺服器端渲染（Nuxt SSR），所有欄位都直接寫在 HTML 裡，
因此不需要 Playwright／Chromium。這很重要：Chromium 在 591 這種重度頁面上
browser + renderer 通常吃掉 300~400MB，加上 Web 程序就會超過 Render 免費方案的
512MB 上限，實際造成連續數日的 Out of memory。改成純 HTTP 之後整個問題消失，
而且 SSR 的 HTML 反而提供了更多欄位（結構化地址、最近捷運站與距離、額外費用）。
"""
import os
import re
import random
import time
import logging
from typing import List, Dict, Any, Optional, Tuple
import requests

from config import USER_AGENTS, COOKIES_FILE, EXCLUDE_KEYWORDS, MIN_SIZE_SQFT
from search_filters import get_target_search_urls, RENT_MIN, RENT_MAX, ALLOWED_SECTIONS
from text_features import detect_couples_warnings, detect_couples_features

logger = logging.getLogger(__name__)

OFF_MARKET_KEYWORDS = [
    "您查詢的物件不存在",
    "可能已關閉或者被刪除",
    "很抱歉，您查詢的物件不存在",
    "物件已下架",
    "已被租出",
    "找不到頁面",
    "此房屋已下架",
    "此物件不存在",
    "抱歉，您造訪的頁面不存在",
    "已被刪除"
]

def sanitize_text(text: str) -> str:
    if not text:
        return ""
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(text))

def parse_numeric_price(price_str: str) -> int:
    if not price_str:
        return 0
    clean = re.sub(r'[^\d]', '', str(price_str))
    return int(clean) if clean else 0

def parse_sqft(size_str: str) -> float:
    if not size_str:
        return 0.0
    match = re.search(r'(\d+(?:\.\d+)?)', str(size_str))
    return float(match.group(1)) if match else 0.0

# 卡片上的標籤詞，解析標題時要略過
CARD_TAGS = {
    "精選", "優選好屋", "近捷運", "新上架", "租金補貼", "可報稅", "拎包入住", "近商圈",
    "有電梯", "隨時可遷入", "可開伙", "急租", "降價", "免仲介費", "屋主直租",
    "獨立套房", "分租套房", "整層住家", "雅房", "樓中樓", "車位",
}
# 每個搜尋網址最多翻幾頁（591 每頁 30 筆）。頁數越多涵蓋越廣，但請求量也線性增加。
MAX_LIST_PAGES = int(os.getenv("MAX_LIST_PAGES", "3"))

HOUSE_KINDS = {"獨立套房", "分租套房", "整層住家", "雅房", "樓中樓"}


def _visible_lines(block: str) -> List[str]:
    """把一段 HTML 轉成可見文字行。"""
    txt = re.sub(r'<script.*?</script>', '', block, flags=re.S)
    txt = re.sub(r'<style.*?</style>', '', txt, flags=re.S)
    txt = re.sub(r'<[^>]+>', '\n', txt)
    out = []
    for line in txt.split("\n"):
        line = re.sub(r'\s+', ' ', line).strip()
        if not line or line.startswith(("data:image", "http", "data-")):
            continue
        out.append(line)
    return out


def normalize_station(name: str) -> str:
    """「松山機場捷運站2號出口」→「松山機場」，去掉出口與捷運字樣。"""
    n = re.sub(r'\d+號出口.*$', '', (name or "").strip())
    n = re.sub(r'(捷運站|捷運|站)$', '', n)
    return n.strip()


def parse_list_html(html: str) -> List[Dict[str, Any]]:
    """從搜尋結果頁 HTML 取出所有物件。

    591 的列表頁是 SSR，每張卡片是一個 `div.item`，可見文字依序是
    標籤 → 標題 → 房型 → 坪數 → 樓層 → 地址 → 距離捷運 → 仲介 → 租金。
    """
    results: List[Dict[str, Any]] = []
    for raw_block in re.split(r'<div class="item"', html)[1:]:
        block = raw_block[:8000]
        m_id = re.search(r'/(\d{7,8})(?:["\?])', block)
        if not m_id:
            continue
        lines = _visible_lines(block)

        # 租金：在「元/月」那一行往前找第一個合理數字
        price = 0
        for i, line in enumerate(lines):
            if line.startswith("元/月"):
                for back in range(i - 1, max(-1, i - 4), -1):
                    digits = re.sub(r'[^\d]', '', lines[back])
                    if digits and int(digits) >= 3000:
                        price = int(digits)
                        break
                if price:
                    break

        size = next((l for l in lines if re.fullmatch(r'\d+(?:\.\d+)?坪', l)), "")
        floor = next((l for l in lines if re.fullmatch(r'[\dB]+F?/\d+F', l)), "")
        # 地址在卡片上是「中山區-吉林路26巷」這種結構化寫法
        addr_line = next((l for l in lines if re.fullmatch(r'[一-龥]{2,4}區-.+', l)), "")
        address = addr_line.replace("-", "", 1) if addr_line else ""

        station, distance = "", 0
        for i, line in enumerate(lines):
            m = re.fullmatch(r'距(.+)', line)
            if m and i + 1 < len(lines):
                m2 = re.fullmatch(r'(\d+)公尺', lines[i + 1])
                if m2:
                    station = normalize_station(m.group(1))
                    distance = int(m2.group(1))
                    break

        extra_fee = 0
        m_extra = re.search(r'額外費用\s*([\d,]+)\s*元', block)
        if m_extra:
            extra_fee = int(m_extra.group(1).replace(",", ""))

        title = ""
        for line in lines:
            if line in CARD_TAGS or len(line) < 6:
                continue
            if re.fullmatch(r'[\d,]+', line) or "公尺" in line or line.endswith("坪"):
                continue
            title = line
            break

        kind = next((l for l in lines if l in HOUSE_KINDS), "")
        house_id = m_id.group(1)

        results.append({
            "house_id": house_id,
            "title": sanitize_text(title),
            "numeric_price": price,
            "size": size,
            "floor": floor,
            "address": sanitize_text(address),
            "station": station,
            "station_distance": distance,
            "extra_fee": extra_fee,
            "kind": kind,
            "link": f"https://rent.591.com.tw/{house_id}",
            "card_text": sanitize_text(" ".join(lines)),
        })
    return results


def clean_address_string(raw_addr: str) -> str:
    """清理地址字串，剔除『依現場』等前綴與『整層住家出租』等後綴噪訊字詞"""
    if not raw_addr:
        return "未提供地址"
    
    clean = re.sub(r'^(?:依現場|社區名稱|所屬社區|高樓層|電梯大樓|大廈|無|未知)+', '', raw_addr).strip()
    clean = re.sub(r'(?:整層住家出租|整層住家|獨立套房出租|獨立套房|分租套房|雅房|住家出租|住家|出租)+$', '', clean).strip()

    match = re.search(r'((?:[一-龥]{2,3}[市縣])?[一-龥]{2,4}[區市鎮鄉][\s\-–—─]*[一-龥\dA-Za-z]+(?:路|街|段|巷|弄|號|大道)?)', clean)
    if match:
        return match.group(1).replace("-", " ").strip()
    
    dist_match = re.search(r'((?:[一-龥]{2,3}[市縣])?[一-龥]{2,4}[區市鎮鄉])', clean)
    if dist_match:
        return dist_match.group(1).strip()

    return clean if clean else "未提供地址"

class RentalScraper:
    def __init__(self, target_urls: Optional[List[str]] = None,
                 known_prices: Optional[Dict[str, int]] = None):
        self.target_urls = target_urls or get_target_search_urls()
        # {house_id: 已存的租金}。已在資料庫且租金沒變的房源不必再抓內頁：
        # 它既然出現在搜尋結果就代表還在架上，費用細項也早就存過了。
        # 翻頁之後每輪有數百筆，這一步省下絕大多數的請求與記憶體。
        # 租金有變的仍然要抓——降價會連帶更新 details_text，
        # 若用較貧乏的文字覆蓋，費用估算會退步。
        self.known_prices = known_prices or {}

    def contains_exclude_keyword(self, text: str) -> Optional[str]:
        if not text:
            return None
        for kw in EXCLUDE_KEYWORDS:
            if kw in text:
                return kw
        return None

    # 實作已移到 text_features.py，讓 Web 程序不必為了這兩個 regex 而載入 Playwright。
    # 這裡保留同名方法，既有呼叫端不需要改動。
    def detect_couples_warnings(self, text: str) -> List[str]:
        return detect_couples_warnings(text)

    def detect_couples_features(self, text: str) -> List[str]:
        return detect_couples_features(text)

    def is_invalid_broker_or_ad(self, title: str, href: str) -> bool:
        invalid_patterns = [
            "金牌專家", "專家·", "專家", "經紀人", "屋主直租2間", "新上架2間", "直租",
            "社區", "廣場", "民生社區", "體驗", "廣告", "客服", "App"
        ]
        for pat in invalid_patterns:
            if pat in title:
                return True
        if "broker" in href or "community" in href or "building" in href or "cs-ai" in href or "forum" in href:
            return True
        return False

    def is_in_allowed_sections(self, text: str) -> bool:
        if not ALLOWED_SECTIONS:
            return True
        for sec in ALLOWED_SECTIONS:
            if sec in text:
                return True
        return False

    def fetch_detail_info(self, house_id: str, current_addr: str) -> Tuple[str, str, bool]:
        """純 Python HTTP GET 直連 591 內頁，完全不佔用任何 Chromium 記憶體"""
        url = f"https://rent.591.com.tw/{house_id}"
        exact_address = clean_address_string(current_addr)
        details_text = ""
        is_off_market = False
        
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7"
        }

        try:
            resp = requests.get(url, headers=headers, timeout=5)
            resp.encoding = 'utf-8'  # 強制使用 UTF-8 解碼，避免中文亂碼
            if resp.status_code in [404, 410]:
                is_off_market = True
            elif resp.status_code == 200:
                html_text = resp.text
                if any(k in html_text for k in OFF_MARKET_KEYWORDS):
                    is_off_market = True

                meta_match = re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\'\n]+)["\']', html_text)
                if not meta_match:
                    meta_match = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\'\n]+)["\']', html_text)

                if meta_match:
                    desc = meta_match.group(1)
                    city_dist_match = re.search(r'((?:[一-龥]{2,3}[市縣])?[一-龥]{2,4}[區市鎮鄉])', desc)
                    loc_match = re.search(r'位於\s*([^，,：:\n]+)', desc)
                    
                    if city_dist_match and loc_match:
                        dist_str = city_dist_match.group(1).replace("台北市", "").replace("新北市", "").strip()
                        loc_str = loc_match.group(1).strip()
                        if loc_str not in dist_str:
                            exact_address = clean_address_string(f"{dist_str}{loc_str}")
                        else:
                            exact_address = clean_address_string(loc_str)

                if not exact_address or "未提供" in exact_address:
                    m_addr = re.search(r'地\s*址[：:\s]*([^<"\n]+)', html_text)
                    if m_addr:
                        exact_address = clean_address_string(m_addr.group(1).strip())

                # 先用關鍵字篩掉絕大多數行，再對留下來的少數行做 regex，
                # 並在收滿 15 行就停止。原本是先把整頁每一行都跑一次 regex 再篩，
                # 那會為每個房源額外配置一份與整頁等大的字串串列。
                FEE_KEYS = ("費用", "管理費", "電費", "水費", "租金包含", "元/月", "一度", "額外費用")
                fee_lines = []
                for line in html_text.split('\n'):
                    if not line.strip() or not any(k in line for k in FEE_KEYS):
                        continue
                    fee_lines.append(re.sub(r'<[^>]+>', ' ', line).strip())
                    if len(fee_lines) >= 15:
                        break
                details_text = " ".join(fee_lines)

        except Exception as e:
            logger.debug(f"HTTP GET 補充內頁失敗 [{house_id}]: {e}")

        return sanitize_text(exact_address), sanitize_text(details_text), is_off_market

    def fetch_list_page(self, target_url: str) -> str:
        """純 HTTP 取回搜尋結果頁 HTML。"""
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        }
        cookies = {}
        if os.path.exists(COOKIES_FILE):
            try:
                import json as _json
                with open(COOKIES_FILE, "r", encoding="utf-8") as f:
                    cookies = {c["name"]: c["value"] for c in _json.load(f) if "name" in c}
            except Exception:
                cookies = {}
        resp = requests.get(target_url, headers=headers, cookies=cookies, timeout=20)
        resp.encoding = "utf-8"
        if resp.status_code != 200:
            logger.error(f"❌ 列表頁回應 HTTP {resp.status_code}: {target_url}")
            return ""
        return resp.text

    def fetch_all_pages(self, target_url: str) -> List[Dict[str, Any]]:
        """依序抓取搜尋結果的前幾頁。

        591 每頁 30 筆，用 `&page=N` 翻頁。某一頁完全沒有新物件就提早停止，
        避免結果不足時做無謂的請求。
        """
        collected: Dict[str, Dict[str, Any]] = {}
        for page in range(1, MAX_LIST_PAGES + 1):
            url = target_url if page == 1 else f"{target_url}&page={page}"
            try:
                html = self.fetch_list_page(url)
            except Exception as e:
                logger.error(f"❌ 取得列表頁第 {page} 頁失敗: {e}")
                break
            if not html:
                break

            items = parse_list_html(html)
            fresh = [i for i in items if i["house_id"] not in collected]
            for i in items:
                collected.setdefault(i["house_id"], i)
            logger.info(f"   第 {page} 頁：解析 {len(items)} 筆，其中新物件 {len(fresh)} 筆")

            if not fresh:
                break
            if page < MAX_LIST_PAGES:
                time.sleep(random.uniform(0.6, 1.2))
        return list(collected.values())

    def fetch_single_url(self, target_url: str, global_seen_ids: set) -> List[Dict[str, Any]]:
        """抓取單一搜尋網址並套用過濾條件。全程純 HTTP，不啟動任何瀏覽器。"""
        raw_items = self.fetch_all_pages(target_url)
        if not raw_items:
            return []
        logger.info(f"列表頁共解析出 {len(raw_items)} 筆原始物件")
        skipped_detail = 0

        results: List[Dict[str, Any]] = []
        for item in raw_items:
            house_id = item["house_id"]
            if house_id in global_seen_ids:
                continue

            full_text = item["card_text"]
            title = item["title"]

            if self.is_invalid_broker_or_ad(title, item["link"]):
                continue

            ex_kw = self.contains_exclude_keyword(full_text)
            if ex_kw:
                logger.info(f"🚫 包含黑名單關鍵字『{ex_kw}』，自動過濾: [{house_id}] {title[:20]}")
                continue

            sqft = parse_sqft(item["size"])
            if sqft and sqft < MIN_SIZE_SQFT:
                logger.info(f"🚫 坪數 ({sqft} 坪) 小於標準 ({MIN_SIZE_SQFT} 坪)，自動過濾: [{house_id}] {title[:20]}")
                continue

            if not self.is_in_allowed_sections(full_text):
                continue

            if item["numeric_price"] < 5000:
                logger.info(f"🚫 無法解析有效刊登租金: [{house_id}] {title[:20]}")
                continue

            global_seen_ids.add(house_id)

            if self.known_prices.get(house_id) == item["numeric_price"]:
                skipped_detail += 1
                exact_addr, details_text, is_off_market = item["address"], "", False
            else:
                exact_addr, details_text, is_off_market = self.fetch_detail_info(house_id, item["address"])

            # 卡片上的「額外費用」直接併進 details_text，
            # cost_calculator 既有的 regex 就能解析到，不必另外傳參數
            if item["extra_fee"]:
                details_text = f"額外費用 {item['extra_fee']:,} 元/月 {details_text}"

            results.append({
                "house_id": house_id,
                "title": title,
                "price": f"{item['numeric_price']:,}元/月",
                "numeric_price": item["numeric_price"],
                # 卡片上的地址是結構化的，比內頁 og:description 解析出來的更可靠
                "address": item["address"] or exact_addr,
                "size": item["size"] or "未標示坪數",
                "floor": item["floor"],
                "kind": item["kind"],
                "mrt_station": item["station"],
                "mrt_distance": item["station_distance"],
                "link": item["link"],
                "details_text": f"{full_text} {details_text}",
                "status": "off_market" if is_off_market else "active",
            })

        if skipped_detail:
            logger.info(f"⏩ 略過 {skipped_detail} 筆已知且租金未變的內頁抓取")
        return results

    def fetch_all_urls(self) -> List[Dict[str, Any]]:
        urls = self.target_urls
        logger.info(f"啟動純 HTTP 爬蟲（不使用瀏覽器），準備爬取 {len(urls)} 個目標網址...")
        results = []
        global_seen_ids = set()

        for i, target_url in enumerate(urls, start=1):
            logger.info(f"[{i}/{len(urls)}] 載入網址: {target_url}")
            sub_results = self.fetch_single_url(target_url, global_seen_ids)
            results.extend(sub_results)
            logger.info(f"網頁 [{i}] 成功爬取 {len(sub_results)} 筆合格房屋")
            time.sleep(1.0)

        logger.info(f"🎯 所有網址爬取完成，全域共抓取到 {len(results)} 筆合格物件！")
        return results

    # 舊名稱保留，避免其他地方仍在呼叫
    fetch_via_playwright = fetch_all_urls

    def run(self) -> List[Dict[str, Any]]:
        sleep_time = random.uniform(1.5, 3.5)
        logger.info(f"執行前隨機延遲 {sleep_time:.2f} 秒 (Anti-scraping sleep)")
        time.sleep(sleep_time)
        return self.fetch_all_urls()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    scraper = RentalScraper()
    data = scraper.run()
    print(f"爬取完成，共 {len(data)} 筆:")
    for d in data[:3]:
        print(d)
