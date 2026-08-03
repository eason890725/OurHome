# -*- coding: utf-8 -*-
"""記憶體洩漏回歸測試。

    python tests/test_memory_leak.py

守護的是實際發生過的事故：`with sqlite3.connect(...) as conn:` 只會 commit，
**不會 close**，因此每次資料庫操作都洩漏一條連線與它的 page cache。
儀表板每次 /api/houses 都會走到 get_all_houses()，累積下來讓 gunicorn worker
從 103MB 一路長到 441MB，最後撞上 Render 的 512MB 上限被砍掉。
"""
import gc
import os
import shutil
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ["GITHUB_REPO"] = ""          # 純本機模式，測試不連網
os.environ.pop("GITHUB_TOKEN", None)

BOX = os.path.join(tempfile.gettempdir(), "ourhome_leak_test")
shutil.rmtree(BOX, ignore_errors=True)
os.makedirs(BOX)
os.chdir(BOX)

from db import HousingDB  # noqa: E402
import ui_shared  # noqa: E402

failures = []


def check(label, cond, extra=""):
    print(("[OK]   " if cond else "[FAIL] ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        failures.append(label)


def open_connections() -> int:
    """目前還活著的 sqlite3 連線數。"""
    gc.collect()
    return sum(1 for o in gc.get_objects() if isinstance(o, sqlite3.Connection))


db = HousingDB(os.path.join(BOX, "leak.db"))
with db._get_connection() as c:
    for i in range(60):
        c.execute(
            "INSERT INTO houses (house_id,title,price,numeric_price,address,size,details_text,status) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f"L{i}", f"房源{i}", "20,000元/月", 20000, "大安區忠孝東路四段", "10坪",
             "管理費 800 元 電費一度 5 元 " * 20, "active"))

print("── 連線是否確實關閉 ──")
baseline = open_connections()
for _ in range(50):
    db.get_all_houses()
after = open_connections()
check("重複讀取 50 次不會累積連線", after <= baseline + 1, f"{baseline} -> {after}")

baseline = open_connections()
for i in range(30):
    db.set_house_rating(f"L{i}", "like")
    db.get_known_prices()
after = open_connections()
check("寫入與查詢混合操作也不累積", after <= baseline + 1, f"{baseline} -> {after}")

# 例外發生時也必須關閉
baseline = open_connections()
for _ in range(20):
    try:
        with db._get_connection() as conn:
            conn.execute("SELECT * FROM 這個表不存在")
    except Exception:
        pass
after = open_connections()
check("拋出例外時連線仍會關閉", after <= baseline + 1, f"{baseline} -> {after}")

print("\n── 儀表板重複取資料 ──")
baseline = open_connections()
for _ in range(40):
    ui_shared.invalidate_houses_cache()      # 強制每次都真的重建
    ui_shared.get_formatted_houses(db)
after = open_connections()
check("儀表板重複取資料不累積連線", after <= baseline + 1, f"{baseline} -> {after}")

print("\n── context manager 語意仍正確 ──")
with db._get_connection() as conn:
    conn.execute("INSERT INTO houses (house_id,title) VALUES ('TX1','交易測試')")
rows = {r["house_id"] for r in db.get_all_houses()}
check("離開 with 之後有自動 commit", "TX1" in rows)

try:
    with db._get_connection() as conn:
        conn.execute("INSERT INTO houses (house_id,title) VALUES ('TX2','會被回滾')")
        raise RuntimeError("模擬失敗")
except RuntimeError:
    pass
rows = {r["house_id"] for r in db.get_all_houses()}
check("發生例外時會回滾", "TX2" not in rows)

print("\n" + ("記憶體洩漏測試全部通過 ✅" if not failures else f"失敗 {len(failures)} 項 ❌: {failures}"))
sys.exit(1 if failures else 0)
