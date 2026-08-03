# -*- coding: utf-8 -*-
"""驗證重啟不再重複推送、暫存檔不再互相踩踏、爬蟲不會疊加執行。"""
import importlib
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = r"C:\personl\OurHome"
BOX = os.path.join(tempfile.gettempdir(), "ourhome_conc")
shutil.rmtree(BOX, ignore_errors=True)
os.makedirs(BOX)
os.chdir(BOX)
sys.path.insert(0, ROOT)
os.environ["GITHUB_TOKEN"] = "fake"

import db as dbmod  # noqa: E402

failures = []


def check(label, cond, extra=""):
    print(("[OK]   " if cond else "[FAIL] ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        failures.append(label)


CLOUD = [{"house_id": "C1", "title": "雲端房", "user_rating": "like"}]


class Resp:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text

    def json(self):
        return json.loads(self.text)


class Fake:
    """忠實模擬 GitHub：PUT 上去的內容，之後 GET 就會拿到同樣的東西。"""

    def __init__(self):
        self.puts = 0
        self.stored = json.dumps(CLOUD, ensure_ascii=False)

    def get(self, url, **kw):
        if "raw.githubusercontent.com" in url:
            return Resp(200, self.stored)
        return Resp(200, json.dumps({"sha": "s"}))

    def put(self, url, **kw):
        import base64
        self.puts += 1
        self.stored = base64.b64decode(kw["json"]["content"]).decode("utf-8")
        return Resp(201)


fake = Fake()
dbmod.requests = fake

# ── 模擬「重啟」：新程序、_LAST_PUSHED_HASH 歸零，但內容與 GitHub 相同 ──
print("── 重啟後不得重複推送 ──")
d1 = dbmod.HousingDB("r1.db")
d1.sync_backup_json()
first = fake.puts
check("第一次同步會推送", first >= 1, f"{first} 次")

for i in range(4):
    dbmod._LAST_PUSHED_HASH = ""          # 模擬程序重啟後變數歸零
    d = dbmod.HousingDB(f"r{i+2}.db")
    d.sync_backup_json()
check("後續重啟不再重複推送（內容與 GitHub 相同）", fake.puts == first,
      f"{first} -> {fake.puts}")

# 內容真的變了就必須推
with d1._get_connection() as c:
    c.execute("INSERT INTO houses (house_id, title) VALUES ('NEW1','新房子')")
    c.commit()
dbmod._LAST_PUSHED_HASH = ""
d1.sync_backup_json()
check("內容真的改變時仍會推送", fake.puts > first, f"{first} -> {fake.puts}")

# ── 暫存檔帶 PID，不會互相踩踏 ──
print("\n── 暫存檔命名 ──")
import inspect  # noqa: E402
src = inspect.getsource(dbmod.HousingDB.sync_backup_json)
check("暫存檔名含 PID", "os.getpid()" in src)
leftovers = [f for f in os.listdir(BOX) if f.endswith(".tmp")]
check("同步後沒有殘留暫存檔", not leftovers, str(leftovers))

# ── 爬蟲執行鎖 ──
print("\n── 爬蟲執行鎖 ──")
sys.path.insert(0, ROOT)
import run_crawler_standalone as rcs  # noqa: E402
importlib.reload(rcs)


class L:
    def warning(self, *a):
        pass

    def info(self, *a):
        pass


log = L()
check("第一個爬蟲取得執行鎖", rcs._acquire_lock(log) is True)
check("第二個爬蟲被擋下（不會疊加執行）", rcs._acquire_lock(log) is False)
rcs._release_lock()
check("釋放後可再次取得", rcs._acquire_lock(log) is True)

# 殘留鎖（父程序被砍、鎖沒清掉）要能自動接手
with open(rcs.LOCK_FILE, "w", encoding="utf-8") as f:
    json.dump({"pid": 99999, "time": time.time() - rcs.LOCK_STALE_SECONDS - 10}, f)
check("超過時效的殘留鎖會被接手", rcs._acquire_lock(log) is True)
rcs._release_lock()
check("鎖檔已清除", not os.path.exists(rcs.LOCK_FILE))

print("\n" + ("併發與重啟測試通過 ✅" if not failures else f"失敗 {len(failures)} 項 ❌: {failures}"))
sys.exit(1 if failures else 0)
