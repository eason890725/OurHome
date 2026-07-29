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

# 3. 多網址目標解析 (支援在 Render 環境變數中設定 TARGET_URL_1, TARGET_URL_2... 或在 TARGET_URL 用逗號/換行隔開多條網址)
TARGET_URLS = []

# 檢查單一 TARGET_URL 變數 (支援逗號或分號或換行分隔多條網址)
single_target_env = os.getenv("TARGET_URL", "")
if single_target_env:
    for u in single_target_env.replace("\n", ",").replace(";", ",").split(","):
        if u.strip() and u.strip() not in TARGET_URLS:
            TARGET_URLS.append(u.strip())

# 檢查編號 TARGET_URL_1, TARGET_URL_2 ... TARGET_URL_50
for i in range(1, 51):
    url_val = os.getenv(f"TARGET_URL_{i}")
    if url_val and url_val.strip() and url_val.strip() not in TARGET_URLS:
        TARGET_URLS.append(url_val.strip())

# 若完全未設定，使用預設安全搜尋網址
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
