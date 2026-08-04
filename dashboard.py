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
from ui_shared import (get_formatted_houses, invalidate_houses_cache,
                       render_dashboard_html, payload_for_api, compute_etag, etag_matches)

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

    def _send_no_cache_headers(self, etag=None):
        """用 no-cache（不是 no-store）：兩者都會強制重新驗證，
        但 no-store 連帶禁止條件式請求，ETag 就失效了。
        沒有快取標頭時瀏覽器會自行推測，曾造成「新資料 + 舊 JS」——
        手機看得到「低於行情」標籤，電腦卻看不到。"""
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        if etag:
            self.send_header("ETag", etag)

    def _respond(self, body: bytes, content_type: str, etag: str, extra=None):
        """內容未變更就回 304，避免重複傳輸整份內容。"""
        if etag_matches(self.headers.get("If-None-Match"), etag):
            self.send_response(304)
            self._send_no_cache_headers(etag)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self._send_no_cache_headers(etag)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            html = render_dashboard_html(PAGE_TITLE)
            self._respond(html.encode("utf-8"), "text/html; charset=utf-8", compute_etag(html))
        elif self.path == "/healthz":
            self._respond(b"ok", "text/plain", '"ok"')
        elif self.path == "/api/houses":
            payload = payload_for_api(get_formatted_houses_cached())
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._respond(body, "application/json; charset=utf-8", compute_etag(payload),
                          {"Access-Control-Allow-Origin": "*"})
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
