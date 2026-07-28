import os
from dotenv import load_dotenv

load_dotenv()

# 1. 租屋模式設定 ('couple': 雙人同住模式 | 'single': 單人居住模式)
MODE = os.getenv("MODE", "couple").lower()

if MODE == "single":
    DEFAULT_ELECTRICITY_KWH = 200
    MIN_SIZE_SQFT = 4.0
    EXCLUDE_KEYWORDS = ["頂加", "暗房", "頂樓加蓋", "事故屋", "凶宅"]
    MODE_LABEL = "單人居住"
else:
    DEFAULT_ELECTRICITY_KWH = 400
    MIN_SIZE_SQFT = 7.0
    EXCLUDE_KEYWORDS = ["限單人", "限單身", "限女", "頂加", "暗房", "頂樓加蓋", "事故屋", "凶宅"]
    MODE_LABEL = "雙人同住"

# 2. Discord Webhook
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

# 3. 多網址目標 (支援 TARGET_URL_1, TARGET_URL_2, TARGET_URL_3...)
TARGET_URLS = []
i = 1
while True:
    url_key = f"TARGET_URL_{i}"
    url_val = os.getenv(url_key)
    if not url_val and i == 1:
        url_val = os.getenv("TARGET_URL")
    
    if url_val:
        TARGET_URLS.append(url_val.strip())
        i += 1
    else:
        break

if not TARGET_URLS:
    TARGET_URLS = [
        "https://rent.591.com.tw/list?region=1&section=1,4,5,7,11&kind=2&shape=2&notice=all_sex&rentprice=10000_20000,20000_30000"
    ]

# 4. 定時檢查間隔 (分鐘)
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "10"))

# 5. 資料庫路徑
DB_PATH = os.getenv("DB_PATH", "rentals.db")

# 6. Cookies 檔與 User Agent 池
COOKIES_FILE = os.getenv("COOKIES_FILE", "cookies.json")
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0"
]
