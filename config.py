import os
from dotenv import load_dotenv

load_dotenv()

# 動態解析 .env 中的所有 TARGET_URL (包含 TARGET_URL, TARGET_URL_1, TARGET_URL_2 ...)
TARGET_URLS = []
for key, value in os.environ.items():
    if key.startswith("TARGET_URL") and value.strip():
        if value.strip() not in TARGET_URLS:
            TARGET_URLS.append(value.strip())

# 若 .env 未設定，提供預設網址
if not TARGET_URLS:
    TARGET_URLS = [
        "https://rent.591.com.tw/list?region=1&section=1,4,5,7,11&kind=2&shape=2&notice=all_sex&rentprice=10000_20000,20000_30000",
        "https://rent.591.com.tw/list?region=1&section=4,7,3,11,5&price=10000_20000,20000_30000&kind=1&shape=2&notice=all_sex&layout=1,2"
    ]

# Discord Webhook 設定
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

# 雙人同住黑名單關鍵字
EXCLUDE_KEYWORDS = [
    "限單人", "限單身", "限1人", "限單人入住", "僅限單人",
    "限女性", "限女", "限男生", "限男", 
    "頂加", "頂樓加蓋", "暗房", "無窗"
]

# 雙人同住最小坪數限制 (坪)
MIN_SIZE_SQFT = 7.0

# 資料庫設定
DB_PATH = os.getenv("DB_PATH", "rentals.db")

# 排程間隔 (分鐘)
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "10"))

# Cookies 檔案路徑
COOKIES_FILE = os.getenv("COOKIES_FILE", "cookies.json")

# User-Agent 列表供輪替
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
]
