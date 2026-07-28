import json
import os
import re
import random
import time
import logging
from typing import List, Dict, Any, Optional, Tuple
import requests
from playwright.sync_api import sync_playwright

from config import USER_AGENTS, COOKIES_FILE, EXCLUDE_KEYWORDS, MIN_SIZE_SQFT
from search_filters import get_target_search_urls, RENT_MIN, RENT_MAX, ALLOWED_SECTIONS

logger = logging.getLogger(__name__)

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
    if not raw_addr:
        return "未提供地址"
    clean = raw_addr.replace("依現場社區名稱", "").replace("社區名稱", "").strip()
    match = re.search(r'((?:[\u4e00-\u9fa5]{2,3}[市縣])?[\u4e00-\u9fa5]{2,4}[區市鎮鄉][\s\-–—─]*[\u4e00-\u9fa5\dA-Za-z]+(?:路|街|段|巷|弄|號|大道)?)', clean)
    if match:
        return match.group(1).replace("-", " ").strip()
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

    def fetch_detail_info(self, house_id: str, current_addr: str) -> Tuple[str, str]:
        """使用輕量級 HTTP 請求在 0.3 秒內迅速擷取內頁真實街道地址與費用細項（極速省記憶體）"""
        url = f"https://rent.591.com.tw/{house_id}"
        exact_address = clean_address_string(current_addr)
        details_text = ""
        
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7"
        }

        try:
            resp = requests.get(url, headers=headers, timeout=5)
            if resp.status_code == 200:
                html_text = resp.text
                
                # 1. 抓取真實地址
                m_addr = re.search(r'地\s*址[：:\s]*([^<"\n]+)', html_text)
                if m_addr:
                    exact_address = clean_address_string(m_addr.group(1).strip())
                else:
                    m_map = re.search(r'((?:[\u4e00-\u9fa5]{2,3}[市縣])?[\u4e00-\u9fa5]{2,4}[區市鎮鄉][\s\-–—─]*[\u4e00-\u9fa5\dA-Za-z]+(?:路|街|段|巷|弄|號|大道)?)', html_text)
                    if m_map:
                        exact_address = clean_address_string(m_map.group(1).strip())

                # 2. 抓取費用細項說明
                clean_lines = [re.sub(r'<[^>]+>', ' ', l).strip() for l in html_text.split('\n') if l.strip()]
                fee_lines = [l for l in clean_lines if any(k in l for k in ["費用", "管理費", "電費", "水費", "租金包含", "元/月", "一度", "額外費用"])]
                details_text = " ".join(fee_lines[:15])

        except Exception as e:
            logger.debug(f"HTTP GET 補充內頁失敗 [{house_id}]: {e}")

        return exact_address, details_text

    def fetch_via_playwright(self) -> List[Dict[str, str]]:
        urls = self.target_urls
        logger.info(f"啟動 Playwright 瀏覽器，準備依序爬取 {len(urls)} 個目標網址...")
        results = []
        global_seen_ids = set()

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-blink-features=AutomationControlled"]
            )
            
            context_options = {
                "user_agent": random.choice(USER_AGENTS),
                "viewport": {"width": 1280, "height": 800},
                "device_scale_factor": 1
            }
            
            if os.path.exists(COOKIES_FILE):
                try:
                    with open(COOKIES_FILE, "r", encoding="utf-8") as f:
                        cookies = json.load(f)
                    context = browser.new_context(**context_options)
                    context.add_cookies(cookies)
                except Exception as e:
                    logger.error(f"載入 cookies.json 失敗: {e}")
                    context = browser.new_context(**context_options)
            else:
                context = browser.new_context(**context_options)

            page = context.new_page()

            for i, target_url in enumerate(urls, start=1):
                try:
                    logger.info(f"[{i}/{len(urls)}] 載入網址: {target_url}")
                    page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(3000)

                    for _ in range(3):
                        page.evaluate("window.scrollBy(0, 1000);")
                        page.wait_for_timeout(800)

                    items = page.eval_on_selector_all(
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

                    url_count = 0
                    for item in items:
                        full_text = item.get("full_text", "")
                        link = item.get("link", "")
                        title = item.get("title", "")
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

                        # 秒級極速補充內頁地址與費用
                        exact_addr, details_text = self.fetch_detail_info(house_id, raw_address)
                        combined_text = f"{full_text} {details_text}"

                        # 精準租金解析（排除「降500元」或「降1200元」之降價標籤干擾）
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
                            "details_text": combined_text
                        }

                        results.append(clean_item)
                        url_count += 1

                    logger.info(f"網頁 [{i}] 成功爬取 {url_count} 筆合格房屋")

                except Exception as e:
                    logger.error(f"爬取網址 [{target_url}] 失敗: {e}")

            browser.close()

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
