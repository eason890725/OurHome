# -*- coding: utf-8 -*-
"""下架偵測測試。

    python tests/test_off_market.py

原本的偵測只在「房源仍出現在搜尋結果、但內頁說已下架」這個很窄的條件下才成立，
實務上房子被租掉就是從搜尋結果消失，因此幾乎從未生效
（實測 246 筆評分過的房源有 233 筆標示在架，抽驗 25 筆發現 9 筆其實已下架）。

改為輪流直接驗證內頁。這裡用假的 requests 驗證判定邏輯與輪替順序，不連外網。
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ["GITHUB_REPO"] = ""
os.environ.pop("GITHUB_TOKEN", None)

BOX = os.path.join(tempfile.gettempdir(), "ourhome_offmarket_test")
shutil.rmtree(BOX, ignore_errors=True)
os.makedirs(BOX)
os.chdir(BOX)

import scraper as scrapermod  # noqa: E402
from db import HousingDB  # noqa: E402

failures = []


def check(label, cond, extra=""):
    print(("[OK]   " if cond else "[FAIL] ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        failures.append(label)


class FakeRaw:
    def __init__(self, data):
        self.data = data

    def read(self, n, decode_content=True):
        return self.data[:n]


class FakeResp:
    def __init__(self, status, body=b""):
        self.status_code = status
        self.raw = FakeRaw(body)

    def close(self):
        pass


class FakeRequests:
    """依 house_id 回傳預先安排好的結果。"""

    def __init__(self, plan):
        self.plan = plan
        self.calls = []

    def get(self, url, **kw):
        hid = url.rstrip("/").split("/")[-1]
        self.calls.append(hid)
        entry = self.plan.get(hid, (200, "正常的房屋頁面內容"))
        status, body = entry
        if isinstance(body, str):
            body = body.encode("utf-8")
        return FakeResp(status, body)


print("── 判定邏輯 ──")
plan = {
    "GONE404": (404, ""),
    "GONE410": (410, ""),
    "GONEKW": (200, "很抱歉，您查詢的物件不存在，可能已關閉或者被刪除"),
    "ALIVE": (200, "獨立套房 12坪 中山區 租金 25,000 元/月"),
    "RATE": (429, ""),
    "ERR5XX": (503, ""),
}
scrapermod.requests = FakeRequests(plan)
s = scrapermod.RentalScraper(target_urls=["https://example.invalid/list"])

check("HTTP 404 判定為已下架", s.check_off_market("GONE404") is True)
check("HTTP 410 判定為已下架", s.check_off_market("GONE410") is True)
check("內文含下架字樣判定為已下架", s.check_off_market("GONEKW") is True)
check("正常頁面判定為在架", s.check_off_market("ALIVE") is False)
# 暫時性錯誤絕對不能當成下架，否則被限流就會把整批房源誤標
check("HTTP 429 回傳「不明」而非下架", s.check_off_market("RATE") is None)
check("HTTP 503 回傳「不明」而非下架", s.check_off_market("ERR5XX") is None)


class BoomRequests:
    def get(self, *a, **kw):
        raise ConnectionError("模擬斷線")


scrapermod.requests = BoomRequests()
check("網路異常回傳「不明」而非下架", s.check_off_market("ANY") is None)
scrapermod.requests = FakeRequests(plan)

print("\n── 狀態寫入 ──")
db = HousingDB(os.path.join(BOX, "off.db"))
with db._get_connection() as c:
    for hid, rating in [("GONE404", "like"), ("ALIVE", "none"), ("GONEKW", "dislike"),
                        ("RATE", "none"), ("KEEP", "none")]:
        c.execute("INSERT INTO houses (house_id,title,status,user_rating) VALUES (?,?,?,?)",
                  (hid, f"房源{hid}", "active", rating))


def status_of(hid):
    return {r["house_id"]: r for r in db.get_all_houses()}[hid]["status"]


check("標記為下架會改變狀態", db.mark_checked("GONE404", True) is True)
check("狀態確實變成 off_market", status_of("GONE404") == "off_market")
check("重複標記同樣狀態不算變更", db.mark_checked("GONE404", True) is False)
check("在架的維持 active", db.mark_checked("ALIVE", False) is False and status_of("ALIVE") == "active")
check("下架後又回到架上會還原", db.mark_checked("GONE404", False) is True
      and status_of("GONE404") == "active")

print("\n── 輪替順序 ──")
# 此時 GONE404 與 ALIVE 已被 mark_checked 過，其餘尚未驗證。
targets = db.get_houses_to_recheck(limit=3)
ids = [t["house_id"] for t in targets]
check("尚未驗證的排在已驗證的前面", "GONE404" not in ids and "ALIVE" not in ids, str(ids))
check("尚未驗證者當中，有評分的優先", ids[0] == "GONEKW", str(ids))
check("數量受 limit 限制", len(targets) == 3, str(len(targets)))

# 剛驗證過的要排到最後，否則輪替永遠覆蓋不到其他房源
db.mark_checked("GONEKW", False)
db.mark_checked("RATE", False)
db.mark_checked("KEEP", False)
later = [t["house_id"] for t in db.get_houses_to_recheck(limit=5)]
check("全部驗證過之後，最早驗證的重新排到最前面", later[0] == "GONE404", str(later))
# 若把評分放第一順位，246 筆評分過的會讓沒評分的永遠輪不到
check("沒評分的房源也會被排進輪替", "KEEP" in later, str(later))

print("\n── 整批驗證 ──")
sys.path.insert(0, ROOT)
from run_crawler_standalone import recheck_off_market  # noqa: E402

scrapermod.requests = FakeRequests(plan)
newly = recheck_off_market(db, s)
check("整批驗證有標記出下架的房源", newly >= 2, f"{newly} 筆")
check("GONE404 被標記", status_of("GONE404") == "off_market")
check("GONEKW 被標記", status_of("GONEKW") == "off_market")
check("ALIVE 仍在架", status_of("ALIVE") == "active")
# 429 的那筆狀態不明，必須保持原狀而不是被誤標下架
check("狀態不明的房源保持原狀", status_of("RATE") == "active")

print("\n── last_checked_at 不可進備份 ──")
houses = db.get_all_houses()
check("資料庫查詢有 last_checked_at 欄位", "last_checked_at" in houses[0])
import json  # noqa: E402
db.sync_backup_json()
with open(os.path.join(BOX, "rentals_backup.json"), encoding="utf-8") as f:
    backup = json.load(f)
check("備份檔不含 last_checked_at（否則每輪都會推 GitHub）",
      "last_checked_at" not in backup[0], str(sorted(backup[0].keys()))[:80])

print("\n" + ("下架偵測測試全部通過 ✅" if not failures else f"失敗 {len(failures)} 項 ❌: {failures}"))
sys.exit(1 if failures else 0)
