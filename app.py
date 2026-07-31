import json
import re
import os
import sys
import time
import random
import logging
import threading
import subprocess
import requests
from flask import Flask, jsonify, request

from db import HousingDB
from scraper import RentalScraper
from notifier import DiscordNotifier
from config import DISCORD_WEBHOOK_URL, DB_PATH, CHECK_INTERVAL_MINUTES
from ui_shared import get_formatted_houses, invalidate_houses_cache, render_dashboard_html

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("OurHomeCloudApp")

app = Flask(__name__)
db = HousingDB(DB_PATH)
notifier = DiscordNotifier(DISCORD_WEBHOOK_URL)
scraper = RentalScraper()

PAGE_TITLE = "OurHome 租屋品質與成本儀表板 (雲端 24H 版)"


def get_formatted_houses_cached():
    return get_formatted_houses(db, scraper)


@app.route("/")
def index():
    return render_dashboard_html(PAGE_TITLE)

@app.route("/api/houses")
def api_houses():
    houses = get_formatted_houses_cached()
    return jsonify(houses)

@app.route("/api/rating", methods=["POST"])
def api_rating():
    try:
        data = request.get_json(force=True)
        house_id = str(data.get("house_id", ""))
        rating = str(data.get("rating", "none"))
        success = db.update_house_rating(house_id, rating, sync_git=True)
        invalidate_houses_cache()
        return jsonify({"success": success, "house_id": house_id, "rating": rating})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/sync_ratings", methods=["POST"])
def api_sync_ratings():
    try:
        data = request.get_json(force=True)
        ratings = data.get("ratings", {})
        synced_count = 0
        changed = False
        for hid, r in ratings.items():
            found, did_change = db.set_house_rating(str(hid), str(r))
            if found:
                synced_count += 1
            changed = changed or did_change
        # 只有真的有評分被改動才同步。儀表板每次開啟都會整包送回來，
        # 若不分辨就同步，等於每開一次頁面就產生一個 GitHub commit。
        if changed:
            db.sync_backup_json()
            invalidate_houses_cache()
        return jsonify({"success": True, "synced_count": synced_count, "changed": changed})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

def keep_render_alive():
    """發送 HTTP Ping 防止 Render 免費伺服器因 15 分鐘無人存取而休眠 (24H 防休眠保活)"""
    try:
        # Render 會自動注入 RENDER_EXTERNAL_URL；本機或其他平台可用 KEEP_ALIVE_URL 指定。
        # 兩者都沒有就不 ping（本機執行時本來也不需要防休眠）。
        render_url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("KEEP_ALIVE_URL")
        if not render_url:
            return
        resp = requests.get(f"{render_url.rstrip('/')}/", timeout=10)
        logger.info(f"⚡ 防休眠 Ping 成功 ({render_url}) [Status: {resp.status_code}]")
    except Exception as e:
        logger.debug(f"防休眠 Ping 提示: {e}")

def background_crawler_loop():
    logger.info("啟動 24H 雲端自動巡邏背景獨立行程機制...")
    time.sleep(90)
    while True:
        try:
            logger.info("=== 喚醒獨立子程序爬蟲 (完全不佔用或鎖定 Web GIL) ===")
            proc = subprocess.Popen([sys.executable, "run_crawler_standalone.py"])
            proc.wait()
        except Exception as e:
            logger.error(f"調用獨立爬蟲失敗: {e}")

        sleep_seconds = CHECK_INTERVAL_MINUTES * 60 + random.randint(-30, 30)
        logger.info(f"巡邏結束，等待 {sleep_seconds} 秒後進行下一次巡邏...")
        
        elapsed = 0
        while elapsed < sleep_seconds:
            time.sleep(300)
            elapsed += 300
            keep_render_alive()

crawler_thread = threading.Thread(target=background_crawler_loop, daemon=True)
crawler_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
