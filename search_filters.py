"""
租屋搜尋條件設定檔
從 .env 及 config.py 自動載入所有目標爬取網址
"""

from config import TARGET_URLS

# 雙人同住白名單行政區 (預設安全校驗)
ALLOWED_SECTIONS = ["大安區", "中山區", "信義區", "松山區", "南港區"]

RENT_MIN = 10000
RENT_MAX = 30000

def get_target_search_urls() -> list:
    """傳回所有從 .env 設定載入的 591 網址列表"""
    return [url.strip() for url in TARGET_URLS if url.strip()]

if __name__ == "__main__":
    urls = get_target_search_urls()
    print(f"目前從 .env 成功載入的 591 搜尋網址共 {len(urls)} 個：")
    for idx, u in enumerate(urls, 1):
        print(f"  [{idx}] {u}")
