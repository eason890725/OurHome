import json
import os
import re
import gc
import random
import time
import logging
from typing import List, Dict, Any, Optional, Tuple
import requests
from playwright.sync_api import sync_playwright

from config import USER_AGENTS, COOKIES_FILE, EXCLUDE_KEYWORDS, MIN_SIZE_SQFT
from search_filters import get_target_search_urls, RENT_MIN, RENT_MAX, ALLOWED_SECTIONS

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

def clean_address_string(raw_addr: str) -> str:
    """清理地址字串，剔除『依現場』等前綴與『整層住家出租』等後綴噪訊字詞"""
    if not raw_addr:
        return "未提供地址"
    
    clean = re.sub(r'^(?:依現場|社區名稱|所屬社區|高樓層|電梯大樓|大廈|無|未知)+', '', raw_addr).strip()
    clean = re.sub(r'(?:整層住家出租|整層住家|獨立套房出租|獨立套房|分租套房|雅房|住家出租|住家|出租)+$', '', clean).strip()

    match = re.search(r'((?:[\u4e00-\u9fa5]{2,3}[市縣])?[\u4e00-\u9fa5]{2,4}[區市鎮鄉][\s\-–—─]*[\u4e00-\u9fa5\dA-Za-z]+(?:路|街|段|巷|弄|號|大道)?)', clean)
    if match:
        return match.group(1).replace("-", " ").strip()
    
    dist_match = re.search(r'((?:[\u4e00-\u9fa5]{2,3}[市縣])?[\u4e00-\u9fa5]{2,4}[區市鎮鄉])', clean)
    if dist_match:
        return dist_match.group(1).strip()

    return clean if clean else "未提供地址"

class RentalScraper:
    def __init__(self, target_urls: Optional[List[str]] = None):
        self.target_urls = target_urls or get_target_search_urls()

    def contains_exclude_keyword(self, text: str) -> Optional[str]:
        if not text:
            return None
        for kw in EXCLUDE_KEYWORDS:
            if kw in text:
                return kw
        return None

    def detect_couples_warnings(self, text: str) -> List[str]:
        warnings = []
        if re.search(r'(第二人|多1人|兩人入住加價|加收費用|加價|每多一人)', text):
            warnings.append("⚠️ 第二人入住需額外加價/補貼")
        if re.search(r'(儲熱式|儲熱型|電熱水器)', text):
            warnings.append("⚠️ 儲熱式熱水器 (連續洗澡熱水可能不足)")
        return warnings

    def detect_couples_features(self, text: str) -> List[str]:
        features = []
        if re.search(r'(獨立陽台|獨陽|有陽台|陽台)', text):
            features.append("🧺 獨立陽台")
        if re.search(r'(獨立洗衣機|獨洗|獨洗獨曬|個人洗衣機)', text):
            features.append("🧺 獨立洗衣機")
        if re.search(r'(雙人床|雙人雙層|雙人大床)', text):
            features.append("🛏️ 雙人床配置")
        return features

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
                    city_dist_match = re.search(r'((?:[\u4e00-\u9fa5]{2,3}[市縣])?[\u4e00-\u9fa5]{2,4}[區市鎮鄉])', desc)
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

                clean_lines = [re.sub(r'<[^>]+>', ' ', l).strip() for l in html_text.split('\n') if l.strip()]
                fee_lines = [l for l in clean_lines if any(k in l for k in ["費用", "管理費", "電費", "水費", "租金包含", "元/月", "一度", "額外費用"])]
                details_text = " ".join(fee_lines[:15])

        except Exception as e:
            logger.debug(f"HTTP GET 補充內頁失敗 [{house_id}]: {e}")

        return sanitize_text(exact_address), sanitize_text(details_text), is_off_market

    def fetch_single_url(self, target_url: str, global_seen_ids: set) -> List[Dict[str, str]]:
        """
        階段一：極速使用 Playwright 擷取清單卡片資訊 (2 秒內完成並立即關閉 Chromium)
        階段二：在純 Python (0 MB 瀏覽器開銷) 環境下完成過濾與 HTTP 內頁補充
        """
        raw_items = []
        ultra_low_memory_args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-accelerated-2d-canvas",
            "--no-first-run",
            "--no-zygote",
            "--disable-gpu",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-breakpad",
            "--disable-component-extensions-with-background-pages",
            "--disable-ipc-flooding-protection",
            "--disable-renderer-backgrounding",
            "--metrics-recording-only",
            "--mute-audio",
            "--no-default-browser-check",
            "--no-pings",
            "--password-store=basic",
            "--use-gl=swiftshader",
            "--v8-cache-options=none",
            "--js-flags=--max-old-space-size=64"
        ]

        # === 階段一：Playwright 純做頁面 DOM 擷取，擷完秒關瀏覽器 ===
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=ultra_low_memory_args)
                context = browser.new_context(
                    user_agent=random.choice(USER_AGENTS),
                    viewport={"width": 1280, "height": 800},
                    device_scale_factor=1
                )
                context.route("**/*", lambda route, req: route.abort() if req.resource_type in ["image", "font", "media", "stylesheet"] else route.continue_())

                if os.path.exists(COOKIES_FILE):
                    try:
                        with open(COOKIES_FILE, "r", encoding="utf-8") as f:
                            context.add_cookies(json.load(f))
                    except Exception:
                        pass

                page = context.new_page()
                page.set_default_timeout(20000)

                try:
                    page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
                except Exception:
                    try:
                        page.goto(target_url, wait_until="commit", timeout=10000)
                    except Exception:
                        pass

                page.wait_for_timeout(500)
                for _ in range(2):
                    page.evaluate("window.scrollBy(0, 1000);")
                    page.wait_for_timeout(200)

                raw_items = page.eval_on_selector_all(
                    "div.item-info-title a, a.item-title",
                    """elements => elements.map(el => {
                        let container = el.closest('section, div.item, div.list-item, div.rent-item, div.item-info') || el.parentElement.parentElement;
                        let priceEl = container ? container.querySelector('div.item-info-price, .item-info-price-price, strong, .price') : null;
                        let priceText = priceEl ? priceEl.innerText.trim() : '';
                        let text = container ? container.innerText : el.innerText;
                        
                        return {
                            full_text: text,
                            title: el.innerText.trim(),
                            link: el.href,
                            priceText: priceText
                        };
                    })"""
                )

                browser.close()
        except Exception as e:
            logger.error(f"Playwright 擷取清單頁面失敗 [{target_url}]: {e}")

        # 手動強制回收垃圾
        gc.collect()

        # === 階段二：純 Python (0 MB 瀏覽器開銷) 處理過濾與內頁爬取 ===
        results = []
        for item in raw_items:
            full_text = sanitize_text(item.get("full_text", ""))
            link = item.get("link", "")
            title = sanitize_text(item.get("title", ""))
            price_text = item.get("priceText", "")

            id_match = re.search(r'/\w*?(\d{7,8})', link)
            if not id_match:
                continue

            house_id = id_match.group(1)

            if house_id in global_seen_ids:
                continue

            if self.is_invalid_broker_or_ad(title, link):
                continue

            ex_kw = self.contains_exclude_keyword(full_text)
            if ex_kw:
                logger.info(f"🚫 包含黑名單關鍵字『{ex_kw}』，自動過濾: [{house_id}] {title[:20]}")
                continue

            size_match = re.search(r'(\d+(?:\.\d+)?)\s*坪', full_text)
            if size_match:
                sqft = float(size_match.group(1))
                if sqft < MIN_SIZE_SQFT:
                    logger.info(f"🚫 坪數 ({sqft} 坪) 小於標準 ({MIN_SIZE_SQFT} 坪)，自動過濾: [{house_id}] {title[:20]}")
                    continue

            if not self.is_in_allowed_sections(full_text):
                continue

            global_seen_ids.add(house_id)

            raw_address = ""
            addr_match = re.search(r'((?:[\u4e00-\u9fa5]{2,3}[市縣])?[\u4e00-\u9fa5]{2,4}[區市鎮鄉][\s\-–—─]*[\u4e00-\u9fa5\dA-Za-z]+(?:路|街|段|巷|弄|號|大道)?)', full_text)
            if addr_match:
                raw_address = addr_match.group(1)

            exact_addr, details_text, is_off_market = self.fetch_detail_info(house_id, raw_address)
            combined_text = f"{full_text} {details_text}"

            real_price = 0
            dom_p_match = re.search(r'([\d,]+)\s*元', price_text)
            if dom_p_match:
                parsed_dom_p = parse_numeric_price(dom_p_match.group(1))
                if parsed_dom_p >= 5000:
                    real_price = parsed_dom_p

            if real_price == 0:
                clean_text = re.sub(r'(?:今日|已)?降[\d,]+元|下降[\d,]+元', '', combined_text)
                all_prices = re.findall(r'([\d,]+)\s*元', clean_text)
                valid_prices = [parse_numeric_price(p) for p in all_prices if parse_numeric_price(p) >= 5000]
                if valid_prices:
                    real_price = valid_prices[0]

            if real_price == 0:
                logger.info(f"🚫 無法解析有效刊登租金 (＜ 5,000 元)，自動過濾標籤干擾頁面: [{house_id}] {title[:20]}")
                continue

            price_str = f"{real_price:,}元/月"
            size_str = f"{size_match.group(1)}坪" if size_match else "未標示坪數"

            clean_item = {
                "house_id": house_id,
                "title": title.split("\n")[0].strip(),
                "price": price_str,
                "numeric_price": real_price,
                "address": exact_addr,
                "size": size_str,
                "link": f"https://rent.591.com.tw/{house_id}",
                "details_text": combined_text,
                "status": "off_market" if is_off_market else "active"
            }

            results.append(clean_item)

        return results

    def fetch_via_playwright(self) -> List[Dict[str, str]]:
        urls = self.target_urls
        logger.info(f"啟動二階段極速兩秒脫離 Chromium 隔離架構，準備爬取 {len(urls)} 個目標網址...")
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

    def run(self) -> List[Dict[str, str]]:
        sleep_time = random.uniform(1.5, 3.5)
        logger.info(f"執行前隨機延遲 {sleep_time:.2f} 秒 (Anti-scraping sleep)")
        time.sleep(sleep_time)
        return self.fetch_via_playwright()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    scraper = RentalScraper()
    data = scraper.run()
    print(f"爬取完成，共 {len(data)} 筆:")
    for d in data[:3]:
        print(d)
