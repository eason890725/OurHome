import json
import os
import re
import random
import time
import logging
from typing import List, Dict, Any, Optional
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
    """清理地址字串，剔除『依現場社區名稱』等前綴廣告詞"""
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

    def fetch_detail_exact_address(self, page, house_id: str, current_addr: str) -> str:
        """點入內頁或讀取 591 內頁『位置與周邊』地 址 區塊提取精準真實地址"""
        if current_addr and current_addr != "未提供地址" and "依現場" not in current_addr:
            return clean_address_string(current_addr)
            
        url = f"https://rent.591.com.tw/{house_id}"
        try:
            detail_page = page.context.new_page()
            detail_page.goto(url, wait_until="domcontentloaded", timeout=15000)
            detail_page.wait_for_timeout(1800)

            # DOM 選取器優先提取
            elem = detail_page.query_selector("div.load-map, span.load-map, div.address-info, span.address")
            if elem:
                exact = elem.inner_text().strip()
                detail_page.close()
                return clean_address_string(exact)

            # Regex 備用提取
            body_text = detail_page.inner_text("body")
            m = re.search(r'地\s*址[：:\s]*([\u4e00-\u9fa5A-Za-z0-9\s\-─—–巷弄號段路街區市縣]+)', body_text)
            detail_page.close()
            if m:
                exact = m.group(1).split("\n")[0].strip()
                return clean_address_string(exact)

        except Exception as e:
            logger.debug(f"內頁地址補充失敗 [{house_id}]: {e}")
            
        return clean_address_string(current_addr)

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

            for idx, url in enumerate(urls, 1):
                logger.info(f"[{idx}/{len(urls)}] 載入網址: {url}")
                time.sleep(random.uniform(1.0, 2.0))

                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(3500)

                    anchors = page.query_selector_all("a[href*='rent.591.com.tw/'], a[href*='detail'], a[href^='/']")
                    url_results_count = 0

                    for a in anchors:
                        href = a.get_attribute("href") or ""
                        text = a.inner_text().strip()

                        match = re.search(r'(?:detail/|rent\.591\.com\.tw/|/)?(\d{7,10})', href)
                        if match:
                            house_id = match.group(1)
                            if house_id in global_seen_ids:
                                continue

                            clean_title = text.split("\n")[0].strip()

                            if self.is_invalid_broker_or_ad(clean_title, href):
                                continue

                            if len(clean_title) <= 2 or "圖" in clean_title or "比較" in clean_title:
                                continue

                            parent_text = ""
                            try:
                                parent = a.evaluate_handle("node => node.closest('section, article, div.item-info, div.vue-list-rent-item, li')").as_element()
                                if parent:
                                    parent_text = parent.inner_text()
                            except Exception:
                                pass

                            full_check_text = f"{clean_title}\n{parent_text}"

                            if not self.is_in_allowed_sections(full_check_text):
                                continue

                            hit_kw = self.contains_exclude_keyword(full_check_text)
                            if hit_kw:
                                logger.info(f"🚫 命中雙人黑名單『{hit_kw}』，自動跳過: [{house_id}] {clean_title}")
                                continue

                            size = "未提供坪數"
                            size_match = re.search(r'(\d+(?:\.\d+)?\s*坪)', parent_text)
                            size_num = 0.0
                            if size_match:
                                size = size_match.group(1)
                                size_num = parse_sqft(size)

                            if size_num > 0 and size_num < MIN_SIZE_SQFT:
                                logger.info(f"🚫 坪數 ({size_num} 坪) 小於雙人空間下限 ({MIN_SIZE_SQFT} 坪)，自動跳過: [{house_id}] {clean_title}")
                                continue

                            price = "未提供租金"
                            price_num = 0
                            price_match = re.search(r'([\d,]+\s*元/月|[\d,]+\s*元)', parent_text)
                            if price_match:
                                price = price_match.group(1)
                                price_num = parse_numeric_price(price)

                            if RENT_MIN > 0 and price_num > 0 and price_num < RENT_MIN:
                                continue
                            if RENT_MAX > 0 and price_num > RENT_MAX:
                                continue

                            couples_warnings = self.detect_couples_warnings(full_check_text)
                            couples_features = self.detect_couples_features(full_check_text)

                            raw_address = "未提供地址"
                            if parent_text:
                                lines = [l.strip() for l in parent_text.split("\n") if l.strip()]
                                for line in lines:
                                    addr_m = re.search(r'((?:[\u4e00-\u9fa5]{2,3}[市縣])?[\u4e00-\u9fa5]{2,4}[區市鎮鄉][\s\-–—─]*[\u4e00-\u9fa5\dA-Za-z]+(?:路|街|段|巷|弄|號|大道)?)', line)
                                    if addr_m:
                                        raw_address = addr_m.group(1).replace("-", " ").strip()
                                        break

                            # 雙重精準校驗：提取真實內頁完整地址
                            exact_address = self.fetch_detail_exact_address(page, house_id, raw_address)

                            global_seen_ids.add(house_id)
                            full_link = f"https://rent.591.com.tw/{house_id}"

                            results.append({
                                "house_id": house_id,
                                "title": clean_title,
                                "price": price,
                                "address": exact_address,
                                "size": size,
                                "link": full_link,
                                "couples_warnings": couples_warnings,
                                "couples_features": couples_features
                            })
                            url_results_count += 1

                    logger.info(f"網址 [{idx}] 成功提取 {url_results_count} 筆物件")

                except Exception as page_err:
                    logger.error(f"爬取網址 [{idx}] 時發生錯誤: {page_err}")

            browser.close()

        logger.info(f"🎯 所有網址爬取完畢，總計共成功提取 {len(results)} 筆含真實完整地址之雙人合格物件！")
        return results

    def run(self) -> List[Dict[str, str]]:
        sleep_time = random.uniform(1.0, 2.5)
        logger.info(f"執行前隨機延遲 {sleep_time:.2f} 秒 (Anti-scraping sleep)")
        time.sleep(sleep_time)

        return self.fetch_via_playwright()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    scraper = RentalScraper()
    items = scraper.run()
    print(f"抓取到 {len(items)} 筆含真實地址之雙人合格物件:")
    for item in items[:5]:
        print(item)
