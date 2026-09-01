# -*- coding: utf-8 -*-
"""隱藏已下架房源後，收合邏輯不能連帶藏掉還租得到的房子。

    python tests/test_hide_off_market.py

儀表板改成「已下架不顯示」之後出現一個洞：重複刊登群的主物件下架、
底下的重複刊登卻還在架時，整群會一起消失。線上實測有 23 組是這種情況。
collapse_duplicates() 因此會把在架的那筆換上來當主物件。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ["GITHUB_REPO"] = ""
os.environ.pop("GITHUB_TOKEN", None)

from ui_shared import collapse_duplicates  # noqa: E402

failures = []


def check(label, cond, extra=""):
    print(("[OK]   " if cond else "[FAIL] ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        failures.append(label)


def house(hid, status="active", dup_of=None, rating="none"):
    return {
        "house_id": hid, "status": status, "duplicate_of": dup_of,
        "title": f"物件 {hid}", "price": "24,000元/月", "size": "8.2坪",
        "address": "中山區測試路1號", "link": f"https://example.com/{hid}",
        "user_rating": rating,
    }


print("== 主物件下架、重複刊登還在架 ==")
res = collapse_duplicates([
    house("A", "off_market"),
    house("B", "active", dup_of="A"),
    house("C", "off_market", dup_of="A"),
])
check("只回傳一筆主物件", len(res) == 1, str([h["house_id"] for h in res]))
primary = res[0]
check("在架的 B 被換上來當主物件", primary["house_id"] == "B", primary["house_id"])
check("主物件是在架狀態，不會被隱藏", primary["status"] == "active")
dup_ids = sorted(d["house_id"] for d in primary["duplicates"])
check("A 與 C 都收在底下沒被丟掉", dup_ids == ["A", "C"], str(dup_ids))

print("\n== 整群都下架時不做任何調換 ==")
res = collapse_duplicates([
    house("D", "off_market"),
    house("E", "off_market", dup_of="D"),
])
check("主物件仍是 D", res[0]["house_id"] == "D")
check("狀態維持下架（會被隱藏，這是對的）", res[0]["status"] == "off_market")

print("\n== 主物件在架時維持原狀 ==")
res = collapse_duplicates([
    house("F", "active"),
    house("G", "off_market", dup_of="F"),
])
check("主物件仍是 F", res[0]["house_id"] == "F")
check("下架的 G 收在底下", [d["house_id"] for d in res[0]["duplicates"]] == ["G"])

print("\n== 評分不會因為調換而消失 ==")
res = collapse_duplicates([
    house("H", "off_market", rating="like"),
    house("I", "active", dup_of="H"),
])
check("被換下去的 H 仍帶著 like 評分",
      res[0]["duplicates"][0]["user_rating"] == "like",
      str(res[0]["duplicates"][0]))

print("\n" + ("全部通過" if not failures else f"失敗 {len(failures)} 項: {failures}"))
sys.exit(1 if failures else 0)
