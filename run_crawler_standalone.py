import sys
import os
import time
import json
import logging

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# 巡邏執行鎖。父程序若被重啟，Popen 出來的子程序不一定會被回收，
# 累積下來會有多個爬蟲同時跑，互相搶記憶體、也會搶著寫備份檔。
LOCK_FILE = "crawler.lock"
LOCK_STALE_SECONDS = 900          # 超過 15 分鐘視為殘留鎖，直接接手


def _acquire_lock(logger) -> bool:
    """取得單一執行權。已有其他爬蟲在跑就回傳 False。"""
    now = time.time()
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r", encoding="utf-8") as f:
                info = json.load(f)
            age = now - float(info.get("time", 0))
            if age < LOCK_STALE_SECONDS:
                logger.warning(
                    f"⏭️ 已有另一個爬蟲在執行中（PID {info.get('pid')}，{age:.0f} 秒前啟動），本次跳過。"
                )
                return False
            logger.warning(f"🔓 發現殘留的執行鎖（{age:.0f} 秒前），直接接手。")
        except Exception:
            pass                   # 鎖檔壞掉就當作沒有

    try:
        with open(LOCK_FILE, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "time": now}, f)
        return True
    except Exception as e:
        logger.warning(f"無法建立執行鎖（不影響巡邏）: {e}")
        return True


def _release_lock():
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except Exception:
        pass

from db import HousingDB
from scraper import RentalScraper
from notifier import DiscordNotifier
from cost_calculator import parse_rental_costs
from config import DISCORD_WEBHOOK_URL, DB_PATH
import memlog

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("StandaloneCrawler")

def main():
    logger.info("=== 獨立子程序爬蟲啟動 ===")
    if not _acquire_lock(logger):
        return
    db = HousingDB(DB_PATH)
    # 把既有租金交給爬蟲，讓它略過「已知且租金未變」的內頁抓取
    scraper = RentalScraper(known_prices=db.get_known_prices())
    notifier = DiscordNotifier(DISCORD_WEBHOOK_URL)
    memlog.log_now("爬蟲啟動")

    try:
        scraped_houses = scraper.run()
        if scraped_houses:
            logger.info(f"爬蟲成功抓取到 {len(scraped_houses)} 筆合格物件，準備批量寫入資料庫...")

            # 估算真實月總成本與雙人標籤，Discord 卡片才不會顯示「未估算」
            for house in scraped_houses:
                full_text = f"{house.get('title', '')} {house.get('address', '')} {house.get('details_text', '')}"
                house["cost_info"] = parse_rental_costs(full_text, house.get("price", "0"))
                house["couples_warnings"] = scraper.detect_couples_warnings(full_text)
                house["couples_features"] = scraper.detect_couples_features(full_text)

            # 自動重試機制 (防止 SQLite 瞬時併發競爭)
            batch_result = None
            for attempt in range(3):
                try:
                    batch_result = db.process_houses_batch(scraped_houses)
                    break
                except Exception as db_err:
                    logger.warning(f"⚠️ 批量寫入資料庫第 {attempt + 1} 次重試 (原因: {db_err})")
                    time.sleep(2)

            if batch_result:
                new_houses = batch_result.get("new_houses", [])
                price_drop_houses = batch_result.get("price_drop_houses", [])

                logger.info(f"巡邏結果 -> 新增物件: {len(new_houses)} 筆 | 降價物件: {len(price_drop_houses)} 筆")

                for house in new_houses:
                    notifier.notify_new_house(house)

                for house in price_drop_houses:
                    notifier.notify_price_drop(house, house.get("old_price", ""), house.get("drop_amount", ""))

                if not new_houses and not price_drop_houses:
                    logger.info("沒有新增加或降價的合格物件，不發送通知。")
            else:
                logger.error("❌ 批量寫入資料庫多次重試後失敗。")

    except Exception as e:
        logger.error(f"獨立子程序巡邏異常: {e}")
    finally:
        memlog.log_now("爬蟲結束")
        _release_lock()

if __name__ == "__main__":
    main()
