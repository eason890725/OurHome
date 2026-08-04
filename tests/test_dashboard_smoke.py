# -*- coding: utf-8 -*-
"""儀表板煙霧測試：真的把 dashboard 伺服器跑起來發 HTTP 請求。

    python tests/test_dashboard_smoke.py

工作目錄設在系統暫存區的沙箱，restore_from_backup_json() 只會動到沙箱裡的
rentals_backup.json，不會碰到專案裡的真實資料。需要能連上 GitHub 才能取得測試資料。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOX = os.path.join(tempfile.gettempdir(), "ourhome_smoke_box")
PORT = 5199

if os.path.exists(BOX):
    shutil.rmtree(BOX)
os.makedirs(BOX)
os.chdir(BOX)
sys.path.insert(0, ROOT)

os.environ["DB_PATH"] = os.path.join(BOX, "smoke.db")
os.environ.pop("GITHUB_TOKEN", None)      # 絕不寫回 GitHub
os.environ["DISCORD_WEBHOOK_URL"] = ""    # 絕不發 Discord

failures = []


def check(label, cond, extra=""):
    print(("[OK]   " if cond else "[FAIL] ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        failures.append(label)


# ── dashboard.py（本地 http.server）──
import dashboard  # noqa: E402

threading.Thread(target=dashboard.run_dashboard_server, args=(PORT,), daemon=True).start()
time.sleep(1.5)

html = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=10).read().decode("utf-8")
check("dashboard / 回傳 HTML", html.startswith("<!DOCTYPE html>"), f"{len(html)} 字元")
check("dashboard 標題正確", "<title>OurHome 租屋品質與成本儀表板</title>" in html)
check("無殘留樣板佔位符", "__PAGE_TITLE__" not in html)

# 沒有 Cache-Control 時瀏覽器會自行推測快取時間，改版後就會出現
# 「新資料 + 舊 JS」——實際發生過：手機看得到新標籤，電腦卻看不到。
html_resp = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=10)
cc = html_resp.headers.get("Cache-Control", "")
check("HTML 有送出禁止快取的標頭", "no-store" in cc or "no-cache" in cc, repr(cc))

check("HTML 有 ETag", bool(html_resp.headers.get("ETag")), repr(html_resp.headers.get("ETag")))
# no-store 會連帶禁止條件式請求，ETag 就失效了，因此必須是 no-cache
check("HTML 用的是 no-cache 而非 no-store", "no-store" not in cc, repr(cc))

resp = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/houses", timeout=15)
api_etag = resp.headers.get("ETag")
check("API 也有禁止快取的標頭", "no-cache" in resp.headers.get("Cache-Control", ""),
      repr(resp.headers.get("Cache-Control")))
check("API 有 ETag", bool(api_etag), repr(api_etag))
houses = json.loads(resp.read().decode("utf-8"))


def conditional_status(path, etag):
    """帶 If-None-Match 再取一次，內容沒變應該回 304。"""
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}")
    req.add_header("If-None-Match", etag)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, len(r.read())
    except urllib.error.HTTPError as e:
        return e.code, 0


# 資料每 10 分鐘才變一次，但儀表板輪詢頻率高得多。
# 沒有 304 的話等於每次都整包重傳，免費頻寬 5GB 很快就會用完。
st, ln = conditional_status("/api/houses", api_etag)
check("API 內容未變時回 304", st == 304, f"HTTP {st}")
check("304 不傳內容", ln == 0, f"{ln} bytes")
st, ln = conditional_status("/", html_resp.headers.get("ETag"))
check("HTML 內容未變時回 304", st == 304, f"HTTP {st}")

hz = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/healthz", timeout=10)
hz_body = hz.read()
check("/healthz 可用且極輕量", hz.status == 200 and len(hz_body) < 64,
      f"{len(hz_body)} bytes")

check("API 不傳 price_history（前端未使用）", "price_history" not in houses[0],
      str(sorted(houses[0].keys()))[:70])
check("API 的 details_text 有截斷", len(houses[0].get("details_text") or "") <= 300,
      str(len(houses[0].get("details_text") or "")))
check("/api/houses 是合法 JSON", isinstance(houses, list), f"{len(houses)} 筆")
check("/api/houses Content-Type 正確",
      resp.headers.get("Content-Type", "").startswith("application/json"))
if houses:
    h = houses[0]
    check("房屋物件含 cost_info", isinstance(h.get("cost_info"), dict))
    check("cost_info 有總成本字串", bool(h.get("cost_info", {}).get("total_estimated_cost_str")),
          str(h.get("cost_info", {}).get("total_estimated_cost_str")))
    check("含雙人標籤欄位",
          isinstance(h.get("couples_warnings"), list) and isinstance(h.get("couples_features"), list))

# ── app.py：flask 只裝在 Render 上，本機沒有就退回靜態驗證 ──
app_src = io.open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
try:
    import flask  # noqa: F401
    have_flask = True
except ImportError:
    have_flask = False

if have_flask:
    import app as cloud_app
    client = cloud_app.app.test_client()
    r = client.get("/")
    body = r.get_data(as_text=True)
    check("flask / 回傳 200", r.status_code == 200)
    check("flask /api/houses 回傳 200", client.get("/api/houses").status_code == 200)
else:
    print("[skip] 本機未安裝 flask，改用靜態驗證 app.py")
    import ast
    import ui_shared
    tree = ast.parse(app_src)
    page_title = next(n.value.value for n in tree.body
                      if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "PAGE_TITLE")
    body = ui_shared.render_dashboard_html(page_title)
    check("app.py index() 使用共用模板",
          "render_dashboard_html(PAGE_TITLE)" in app_src)
    check("app.py 有 ETag 條件式回應", "If-None-Match" in app_src)
    check("app.py 有 /healthz 端點", "/healthz" in app_src)
    check("app.py 已無內嵌 HTML_TEMPLATE", "HTML_TEMPLATE" not in app_src)

check("app.py 端標題為雲端版",
      "<title>OurHome 租屋品質與成本儀表板 (雲端 24H 版)</title>" in body)
check("兩個入口的 HTML 只差標題",
      body.replace("OurHome 租屋品質與成本儀表板 (雲端 24H 版)",
                   "OurHome 租屋品質與成本儀表板") == html)
check("dashboard.py 已無內嵌 HTML_TEMPLATE",
      "HTML_TEMPLATE" not in io.open(os.path.join(ROOT, "dashboard.py"), encoding="utf-8").read())

# ── WAL checkpoint ──
from db import HousingDB  # noqa: E402

sdb = HousingDB(os.environ["DB_PATH"])
wal = os.environ["DB_PATH"] + "-wal"
with sdb._get_connection() as c:
    c.execute("INSERT OR REPLACE INTO houses (house_id, title) VALUES ('smoke-1','煙霧測試')")
    c.commit()
before = os.path.getsize(wal) if os.path.exists(wal) else 0
sdb.checkpoint_wal()
after = os.path.getsize(wal) if os.path.exists(wal) else 0
check("checkpoint_wal() 有截斷 WAL", after <= before, f"{before} -> {after} bytes")
with sdb._get_connection() as c:
    row = c.execute("SELECT title FROM houses WHERE house_id='smoke-1'").fetchone()
check("checkpoint 後資料仍在", row is not None and row["title"] == "煙霧測試")

# ── 費用解析（Discord 卡片的資料來源）──
from cost_calculator import parse_rental_costs  # noqa: E402
from scraper import RentalScraper  # noqa: E402

s = RentalScraper()
ft = "近捷運雙人套房 獨立陽台 大安區羅斯福路三段 管理費：1200 元 電費一度 5 元 儲熱式熱水器"
ci = parse_rental_costs(ft, "23,000元/月")
check("details_text 有被解析到管理費", ci["management_fee"] == 1200, str(ci["management_desc"]))
check("雙人警示有偵測到儲熱式", any("儲熱" in w for w in s.detect_couples_warnings(ft)))
check("雙人配備有偵測到陽台", any("陽台" in f for f in s.detect_couples_features(ft)))

print("\n" + ("煙霧測試全部通過 ✅" if not failures else f"失敗 {len(failures)} 項 ❌: {failures}"))
sys.exit(1 if failures else 0)
