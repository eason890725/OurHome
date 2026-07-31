import os
import time
import logging
import random
import threading
import subprocess
import schedule
from dotenv import load_dotenv

from config import DISCORD_WEBHOOK_URL, DB_PATH, CHECK_INTERVAL_MINUTES
from db import HousingDB
from notifier import DiscordNotifier
from scraper import RentalScraper
from cost_calculator import parse_rental_costs
from dashboard import run_dashboard_server, PORT

# 配置 Logging 日誌紀錄
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("scraper.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("MainScheduler")

def auto_git_pull():
    """自動從 GitHub 拉取 Render 雲端同步的最新資料庫與評價紀錄。

    先丟棄本地對 rentals_backup.json 的修改再 pull，避免本地舊資料與雲端衝突。
    若這個資料夾不是 git repo（例如朋友拿到的獨立打包版），整段直接跳過。
    """
    if not os.path.isdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".git")):
        return
    try:
        subprocess.run(["git", "checkout", "rentals_backup.json"], capture_output=True, text=True, timeout=5)
        res = subprocess.run(["git", "pull", "origin", "main"], capture_output=True, text=True, timeout=10)
        if "Already up to date" not in res.stdout and res.returncode == 0:
            logger.info(f"🔄 [自動 GitHub 雙向同步] 已拉取雲端最新資料: {res.stdout.strip()}")
    except Exception as e:
        logger.debug(f"自動 git pull 提示: {e}")

class HousingMonitorApp:
    def __init__(self):
        auto_git_pull()
        self.db = HousingDB(DB_PATH)
        self.notifier = DiscordNotifier(DISCORD_WEBHOOK_URL)
        self.scraper = RentalScraper()

    def check_new_listings(self):
        """核心監控任務：多網址爬取 -> 關鍵字/坪數過濾 -> 特徵碼/地址雙重去重 -> 費用估算 -> Discord 通知"""
        logger.info("=== 開始執行 591 租屋網巡邏 (智慧地址去重與雙人同住模式) ===")
        auto_git_pull()
        try:
            # 1. 多網址抓取與初步關鍵字/坪數過濾
            fetched_houses = self.scraper.run()
            logger.info(f"本次多網址共抓取到 {len(fetched_houses)} 筆合格房屋資料")

            if not fetched_houses:
                logger.warning("未抓取到任何符合條件的資料。")
                return

            # 2. 自動萃取費用細項與估算 400 度用電雙人月總成本
            #    details_text 是內頁抓回來的費用行，一定要納入，否則管理費/電費解析不到
            for house in fetched_houses:
                full_text = f"{house.get('title', '')} {house.get('address', '')} {house.get('details_text', '')}"
                house["cost_info"] = parse_rental_costs(full_text, house.get("price", "0"))
                house["couples_warnings"] = self.scraper.detect_couples_warnings(full_text)
                house["couples_features"] = self.scraper.detect_couples_features(full_text)

            # 3. 資料庫 ID 與智慧地址特徵碼去重/降價檢查
            batch_result = self.db.process_houses_batch(fetched_houses)
            new_houses = batch_result.get("new_houses", [])
            price_drop_houses = batch_result.get("price_drop_houses", [])

            logger.info(f"處理結果 -> 新不重複物件: {len(new_houses)} 筆 | 降價物件: {len(price_drop_houses)} 筆")

            # 4. 處理全新物件 Discord 通知
            if new_houses:
                logger.info(f"準備發送 {len(new_houses)} 筆全新不重複物件通知...")
                for house in new_houses:
                    self.notifier.send_house_card(house, is_price_drop=False)
                    time.sleep(random.uniform(0.8, 1.2))

            # 5. 處理降價警報通知
            if price_drop_houses:
                logger.info(f"🚨 準備發送 {len(price_drop_houses)} 筆降價警報通知...")
                for house in price_drop_houses:
                    self.notifier.send_house_card(house, is_price_drop=True)
                    time.sleep(random.uniform(0.8, 1.2))

            if not new_houses and not price_drop_houses:
                logger.info("沒有全新上架或未刊登物件，亦無既有物件降價，無須發送通知。")

        except Exception as e:
            logger.error(f"檢查任務執行過程中發生異常: {e}", exc_info=True)
        finally:
            logger.info("=== 本次檢查任務結束 ===\n")

    def start_schedule(self, interval_minutes: int = CHECK_INTERVAL_MINUTES):
        """啟動 Web Dashboard 與定時排程迴圈"""
        dashboard_thread = threading.Thread(target=run_dashboard_server, args=(PORT,), daemon=True)
        dashboard_thread.start()
        logger.info(f"🌐 Web 租屋儀表板已於背景啟動：http://localhost:{PORT}")

        logger.info(f"啟動 OurHome 租屋監控排程，設定每 {interval_minutes} 分鐘檢查一次...")
        
        self.check_new_listings()

        schedule.every(interval_minutes).minutes.do(self._scheduled_task)

        while True:
            schedule.run_pending()
            time.sleep(1)

    def _scheduled_task(self):
        jitter_seconds = random.randint(-30, 30)
        if jitter_seconds > 0:
            logger.info(f"排程觸發，隨機延遲 {jitter_seconds} 秒後開始執行...")
            time.sleep(jitter_seconds)
        self.check_new_listings()

if __name__ == "__main__":
    app = HousingMonitorApp()
    app.start_schedule()
