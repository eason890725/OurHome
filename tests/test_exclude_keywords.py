# -*- coding: utf-8 -*-
"""排除關鍵字設定與回溯生效的測試。

    python tests/test_exclude_keywords.py
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ["GITHUB_REPO"] = ""          # 純本機模式，測試不連網
os.environ.pop("GITHUB_TOKEN", None)

BOX = os.path.join(tempfile.gettempdir(), "ourhome_exclude_test")
shutil.rmtree(BOX, ignore_errors=True)
os.makedirs(BOX)
os.chdir(BOX)

failures = []


def check(label, cond, extra=""):
    print(("[OK]   " if cond else "[FAIL] ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        failures.append(label)


# ── 設定解析 ──
print("── .env 設定解析 ──")
import config  # noqa: E402

check("關鍵字字串可依逗號拆解",
      config._split_keywords("頂加,凶宅,樓中樓") == ["頂加", "凶宅", "樓中樓"])
check("分號與頓號也能拆", config._split_keywords("A;B、C") == ["A", "B", "C"])
check("自動去除空白與重複",
      config._split_keywords(" A , A ,  B ") == ["A", "B"])
check("空字串回傳空清單", config._split_keywords("") == [])

# EXTRA_ 追加在預設之後
os.environ["EXTRA_EXCLUDE_KEYWORDS"] = "樓中樓,夾層"
import importlib  # noqa: E402
importlib.reload(config)
check("EXTRA 會追加到預設清單", "樓中樓" in config.EXCLUDE_KEYWORDS and "夾層" in config.EXCLUDE_KEYWORDS,
      str(config.EXCLUDE_KEYWORDS))
check("預設關鍵字仍保留", "凶宅" in config.EXCLUDE_KEYWORDS)

# 完全覆蓋
os.environ["EXCLUDE_KEYWORDS"] = "只要這個"
importlib.reload(config)
check("EXCLUDE_KEYWORDS 會取代預設清單", "凶宅" not in config.EXCLUDE_KEYWORDS)
check("覆蓋後 EXTRA 仍會追加", "樓中樓" in config.EXCLUDE_KEYWORDS, str(config.EXCLUDE_KEYWORDS))

os.environ.pop("EXCLUDE_KEYWORDS")
os.environ.pop("EXTRA_EXCLUDE_KEYWORDS")
importlib.reload(config)

# MIN_SIZE_SQFT 可調
os.environ["MIN_SIZE_SQFT"] = "9.5"
importlib.reload(config)
check("MIN_SIZE_SQFT 可由環境變數指定", config.MIN_SIZE_SQFT == 9.5, str(config.MIN_SIZE_SQFT))
os.environ.pop("MIN_SIZE_SQFT")
importlib.reload(config)

# ── 回溯生效 ──
print("\n── 調整清單後回溯生效 ──")
from db import HousingDB  # noqa: E402

db = HousingDB(os.path.join(BOX, "ex.db"))
with db._get_connection() as c:
    c.execute("""INSERT INTO houses (house_id,title,address,details_text,status)
                 VALUES ('E1','挑高樓中樓套房','大安區','獨立套房 樓中樓 12坪','active')""")
    c.execute("""INSERT INTO houses (house_id,title,address,details_text,status)
                 VALUES ('E2','一般電梯套房','大安區','獨立套房 10坪','active')""")
    c.execute("""INSERT INTO houses (house_id,title,address,details_text,status)
                 VALUES ('E3','頂樓加蓋便宜房','中山區','頂樓加蓋 8坪','active')""")
    c.commit()


def rows():
    return {r["house_id"]: r for r in db.get_all_houses()}


check("初始狀態沒有任何排除標記", all(not r["excluded_by"] for r in rows().values()))

marked, unmarked = db.apply_exclude_keywords(["頂樓加蓋"])
check("只排除命中的那一筆", marked == 1, f"{marked} 筆")
check("E3 被標記且記下命中的關鍵字", rows()["E3"]["excluded_by"] == "頂樓加蓋")
check("E1 未被標記（清單尚未含樓中樓）", rows()["E1"]["excluded_by"] is None)

marked, unmarked = db.apply_exclude_keywords(["頂樓加蓋", "樓中樓"])
check("新增關鍵字後回溯標記 E1", rows()["E1"]["excluded_by"] == "樓中樓", str(rows()["E1"]["excluded_by"]))
check("已標記的不會重複計數", marked == 1, f"{marked} 筆")

marked, unmarked = db.apply_exclude_keywords(["頂樓加蓋", "樓中樓"])
check("內容沒變時不做任何寫入", (marked, unmarked) == (0, 0), str((marked, unmarked)))

marked, unmarked = db.apply_exclude_keywords(["頂樓加蓋"])
check("拿掉關鍵字後解除標記", rows()["E1"]["excluded_by"] is None)
check("解除數正確", unmarked == 1, f"{unmarked} 筆")
check("沒命中的房源始終不受影響", rows()["E2"]["excluded_by"] is None)

# ── 被排除的不進儀表板列表 ──
print("\n── 儀表板列表 ──")
import ui_shared  # noqa: E402

db.apply_exclude_keywords(["頂樓加蓋", "樓中樓"])
listed = ui_shared.collapse_duplicates(db.get_all_houses())
ids = {h["house_id"] for h in listed}
check("被排除的房源不出現在列表", "E1" not in ids and "E3" not in ids, str(sorted(ids)))
check("未被排除的仍在列表", "E2" in ids)
check("資料仍保留在資料庫（沒有被刪除）", len(db.get_all_houses()) == 3)

print("\n" + ("排除關鍵字測試全部通過 ✅" if not failures else f"失敗 {len(failures)} 項 ❌: {failures}"))
sys.exit(1 if failures else 0)
