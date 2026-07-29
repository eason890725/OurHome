import logging
import sys
import os

from scraper import RentalScraper
from db import HousingDB
from cost_calculator import parse_rental_costs
from notifier import DiscordNotifier
from config import DISCORD_WEBHOOK_URL, DB_PATH

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("StandaloneCrawler")

def main():
    logger.info("=== 獨立子程序爬蟲開始執行巡邏任務 (完全不卡死 Web 伺服器) ===")
    try:
        db = HousingDB(DB_PATH)
        notifier = DiscordNotifier(DISCORD_WEBHOOK_URL)
        scraper = RentalScraper()
        
        fetched = scraper.run()
        if fetched:
            for house in fetched:
                full_text = f"{house.get('title', '')} {house.get('address', '')} {house.get('details_text', '')}"
                house["cost_info"] = parse_rental_costs(full_text, house.get("price", "0"))
            
            batch_res = db.process_houses_batch(fetched)
            new_houses = batch_res.get("new_houses", [])
            price_drops = batch_res.get("price_drop_houses", [])
            
            if new_houses:
                notifier.batch_notify(new_houses, is_price_drop=False)
            if price_drops:
                notifier.batch_notify(price_drops, is_price_drop=True)
                
            logger.info("=== 獨立子程序巡邏任務順利完成 ===")
    except Exception as e:
        logger.error(f"獨立子程序巡邏異常: {e}")

if __name__ == "__main__":
    main()
