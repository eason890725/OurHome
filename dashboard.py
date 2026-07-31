import json
import re
import os
import sys
import time
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, Any, List

from db import HousingDB
from config import DB_PATH
from ui_shared import get_formatted_houses, invalidate_houses_cache, render_dashboard_html

logger = logging.getLogger(__name__)

PORT = 5000
db = HousingDB(DB_PATH)

PAGE_TITLE = "OurHome 租屋品質與成本儀表板"

# 儀表板不 import RentalScraper（也就不載入 Playwright），理由同 app.py。


def get_formatted_houses_cached():
    return get_formatted_houses(db)


class DashboardRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(render_dashboard_html(PAGE_TITLE).encode("utf-8"))
        elif self.path == "/api/houses":
            houses = get_formatted_houses_cached()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(houses, ensure_ascii=False).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/rating":
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            try:
                data = json.loads(post_data.decode('utf-8'))
                house_id = str(data.get("house_id", ""))
                rating = str(data.get("rating", "none"))
                success = db.update_house_rating(house_id, rating, sync_git=True)
                invalidate_houses_cache()

                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"success": success, "house_id": house_id, "rating": rating}).encode('utf-8'))
            except Exception as e:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode('utf-8'))
        elif self.path == "/api/sync_ratings":
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            try:
                data = json.loads(post_data.decode('utf-8'))
                ratings = data.get("ratings", {})
                synced_count = 0
                changed = False
                for hid, r in ratings.items():
                    found, did_change = db.set_house_rating(str(hid), str(r))
                    if found:
                        synced_count += 1
                    changed = changed or did_change
                # 只有真的有評分被改動才同步（見 app.py 同段註解）
                if changed:
                    db.sync_backup_json()
                    invalidate_houses_cache()

                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"success": True, "synced_count": synced_count}).encode('utf-8'))
            except Exception as e:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()

def run_dashboard_server(port: int = PORT):
    server_address = ("0.0.0.0", port)
    httpd = HTTPServer(server_address, DashboardRequestHandler)
    logger.info(f"🌐 OurHome Web 儀表板成功啟動！請存取: http://localhost:{port}")
    httpd.serve_forever()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_dashboard_server(PORT)
