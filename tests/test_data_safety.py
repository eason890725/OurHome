# -*- coding: utf-8 -*-
"""資料安全回歸測試：模擬各種網路失敗，確認 db.py 的兩道防線沒有被改壞。

    python tests/test_data_safety.py

全程使用假的 requests，不會真的連到 GitHub；工作目錄設在系統暫存區的沙箱，
不會碰到專案裡的 rentals.db 或 rentals_backup.json。

這裡守護的是「使用者評分不能消失」——房源掉了下次巡邏會重爬回來，評分不會。
"""
import io
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOX = os.path.join(tempfile.gettempdir(), "ourhome_datasafety_box")

if os.path.exists(BOX):
    shutil.rmtree(BOX)
os.makedirs(BOX)
os.chdir(BOX)
sys.path.insert(0, ROOT)

os.environ["GITHUB_TOKEN"] = "fake-token-for-test"

import db as dbmod  # noqa: E402

failures = []


def check(label, cond, extra=""):
    print(("[OK]   " if cond else "[FAIL] ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        failures.append(label)


# 模擬雲端 Master 備份的內容：1001 帶著使用者按過的 like
CLOUD = [{"house_id": "1001", "title": "雲端房源A", "user_rating": "like"},
         {"house_id": "1002", "title": "雲端房源B", "user_rating": "none"}]


class Resp:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text

    def json(self):
        return json.loads(self.text)


class FakeRequests:
    """可程式化的假 requests，記錄所有呼叫以便斷言。"""

    def __init__(self):
        self.raw_mode = "ok"       # ok | http_error | exception
        self.put_queue = []        # 依序回傳的 PUT 狀態碼
        self.put_calls = []
        self.get_calls = []

    def get(self, url, **kw):
        self.get_calls.append(url)
        if "raw.githubusercontent.com" in url:
            if self.raw_mode == "exception":
                raise ConnectionError("模擬斷線")
            if self.raw_mode == "http_error":
                return Resp(404, "Not Found")
            return Resp(200, json.dumps(CLOUD, ensure_ascii=False))
        return Resp(200, json.dumps({"sha": "sha-%d" % len(self.get_calls)}))

    def put(self, url, **kw):
        self.put_calls.append(kw.get("json", {}).get("sha"))
        status = self.put_queue.pop(0) if self.put_queue else 201
        return Resp(status, "simulated %d" % status)


fake = FakeRequests()
dbmod.requests = fake


def fresh(name):
    """各情境用不同的 db 檔名；這裡只清備份檔與計數器
    （SQLite 檔可能還被前一個連線握著，不強行刪除）。"""
    for f in ("rentals_backup.json", "rentals_backup.json.tmp"):
        p = os.path.join(BOX, f)
        if os.path.exists(p):
            os.remove(p)
    dbmod._LAST_PUSHED_HASH = ""
    fake.put_calls.clear()
    fake.get_calls.clear()
    fake.put_queue.clear()
    print("\n── " + name + " ──")


# ═══ 情境 1：Render 重新部署後讀不到雲端主檔，本地也沒備份 ═══
fresh("情境1：雲端讀取失敗 + 本地無備份 → 必須拒絕回推")
fake.raw_mode = "exception"
d = dbmod.HousingDB("t1.db")
check("_master_loaded 為 False", d._master_loaded is False)

# 模擬空 DB 被爬蟲填滿（有房源但完全沒有評分）
with d._get_connection() as c:
    c.execute("INSERT INTO houses (house_id, title, user_rating) VALUES ('9001','新爬到的房子','none')")
    c.commit()
d.sync_backup_json()
check("沒有發出任何 PUT（拒絕以不完整資料覆蓋雲端）", len(fake.put_calls) == 0,
      f"put_calls={fake.put_calls}")
check("沒有寫出 rentals_backup.json", not os.path.exists("rentals_backup.json"))

# ═══ 情境 2：網路恢復 → 自動補讀後才放行 ═══
fresh("情境2：網路恢復 → 補讀成功後才放行回推")
fake.raw_mode = "exception"
d = dbmod.HousingDB("t2.db")
check("一開始 _master_loaded 為 False", d._master_loaded is False)
fake.raw_mode = "ok"
with d._get_connection() as c:
    c.execute("INSERT INTO houses (house_id, title, user_rating) VALUES ('9002','新房','none')")
    c.commit()
d.sync_backup_json()
check("補讀後 _master_loaded 變 True", d._master_loaded is True)
check("有成功 PUT", len(fake.put_calls) == 1, f"put_calls={fake.put_calls}")
pushed = json.loads(io.open("rentals_backup.json", encoding="utf-8").read())
check("推上去的內容含雲端原有資料", {"1001", "1002"} <= {h["house_id"] for h in pushed})
check("雲端的 like 評分沒有被洗掉",
      any(h["house_id"] == "1001" and h["user_rating"] == "like" for h in pushed))

# ═══ 情境 3：推送失敗不可被記成已推送 ═══
fresh("情境3：PUT 失敗 → hash 不可更新，下次要自動重試")
fake.raw_mode = "ok"
d = dbmod.HousingDB("t3.db")
fake.put_queue = [500, 500]
d.sync_backup_json()
check("第一次同步嘗試推送但失敗", len(fake.put_calls) >= 1)
check("失敗後 _LAST_PUSHED_HASH 維持空", dbmod._LAST_PUSHED_HASH == "",
      repr(dbmod._LAST_PUSHED_HASH))

n_before = len(fake.put_calls)
fake.put_queue = [201]
d.sync_backup_json()
check("資料未變動仍會自動重試", len(fake.put_calls) > n_before,
      f"{n_before} -> {len(fake.put_calls)}")
check("成功後才記錄 hash", dbmod._LAST_PUSHED_HASH != "")

n_after = len(fake.put_calls)
d.sync_backup_json()
check("成功之後同樣資料不再重複推送", len(fake.put_calls) == n_after)

# ═══ 情境 4：sha 衝突自動重抓重試 ═══
fresh("情境4：PUT 遇 409 sha 衝突 → 重抓 sha 再試一次")
fake.raw_mode = "ok"
d = dbmod.HousingDB("t4.db")
fake.put_queue = [409, 201]
with d._get_connection() as c:
    c.execute("INSERT INTO houses (house_id, title) VALUES ('9004','觸發變動')")
    c.commit()
d.sync_backup_json()
check("總共嘗試了兩次 PUT", len(fake.put_calls) == 2, f"put_calls={fake.put_calls}")
check("第二次用的是重新抓的 sha", fake.put_calls[0] != fake.put_calls[1], f"{fake.put_calls}")
check("最終成功並記錄 hash", dbmod._LAST_PUSHED_HASH != "")

# ═══ 情境 5：雲端回傳壞內容，不可覆蓋本地好的備份 ═══
fresh("情境5：雲端回傳非 JSON → 不可寫壞本地既有備份")
io.open("rentals_backup.json", "w", encoding="utf-8").write(json.dumps(CLOUD, ensure_ascii=False))


class BrokenRequests(FakeRequests):
    def get(self, url, **kw):
        if "raw.githubusercontent.com" in url:
            return Resp(200, "<html>503 Service Unavailable</html>")
        return super().get(url, **kw)


dbmod.requests = BrokenRequests()
d = dbmod.HousingDB("t5.db")
still = json.loads(io.open("rentals_backup.json", encoding="utf-8").read())
check("本地既有備份沒有被壞內容覆蓋", isinstance(still, list) and len(still) == 2)
check("仍能從本地備份載入並放行", d._master_loaded is True)
dbmod.requests = fake

print("\n" + ("資料安全測試全部通過 ✅" if not failures else f"失敗 {len(failures)} 項 ❌: {failures}"))
sys.exit(1 if failures else 0)
