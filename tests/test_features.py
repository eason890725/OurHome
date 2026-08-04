# -*- coding: utf-8 -*-
"""行政區解析、行情基準線、降價歷史的測試。

    python tests/test_features.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(tempfile.gettempdir())

import ui_shared  # noqa: E402

failures = []


def check(label, cond, extra=""):
    print(("[OK]   " if cond else "[FAIL] ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        failures.append(label)


def house(price, size, address, total_cost, status="active", history=None):
    h = {
        "numeric_price": price, "size": size, "address": address, "status": status,
        "cost_info": {"total_estimated_cost": total_cost},
    }
    if history is not None:
        h["price_history"] = json.dumps(history, ensure_ascii=False)
    return h


# ══════════════ 行情基準線 ══════════════
print("── 行政區解析 ──")
check("一般地址取出行政區", ui_shared.extract_district("中山區民生東路一段") == "中山區")
# 貪婪比對會把「台北市中山區」誤判成「市中山區」，導致同一區被拆成兩組
check("含縣市前綴時仍取出正確行政區",
      ui_shared.extract_district("台北市中山區") == "中山區",
      ui_shared.extract_district("台北市中山區"))
check("新北市地址也正確", ui_shared.extract_district("新北市板橋區文化路") == "板橋區",
      ui_shared.extract_district("新北市板橋區文化路"))
check("有無縣市前綴視為同一區",
      ui_shared.extract_district("台北市大安區忠孝東路") == ui_shared.extract_district("大安區忠孝東路"))
check("解析不出行政區時回傳空字串", ui_shared.extract_district("未提供地址") == "")

print("\n── 行情基準線 ──")

# 大安區 5 筆：每坪 2000/2200/2400/2600/2800 → 中位數 2400
daan = [house(0, "10坪", "大安區忠孝東路四段", u * 10) for u in (2000, 2200, 2400, 2600, 2800)]
baseline = ui_shared.build_market_baseline(daan)
check("樣本足夠時算出該區中位數", baseline.get("大安區") == 2400, str(baseline))

cheap = ui_shared.annotate_market(house(0, "10坪", "大安區忠孝東路四段", 20000), baseline)
check("每坪 2000 判定為明顯低於行情", cheap["level"] == "cheap", str(cheap))
check("偏離百分比正確 (-17%)", cheap["diff_pct"] == -17, str(cheap["diff_pct"]))

fair = ui_shared.annotate_market(house(0, "10坪", "大安區忠孝東路四段", 24000), baseline)
check("每坪 2400 判定為行情價", fair["level"] == "fair", str(fair))

pricey = ui_shared.annotate_market(house(0, "10坪", "大安區忠孝東路四段", 30000), baseline)
check("每坪 3000 判定為明顯高於行情", pricey["level"] == "expensive", str(pricey))

# 樣本不足的行政區
few = daan + [house(0, "10坪", "北投區中央北路", 50000)]
b2 = ui_shared.build_market_baseline(few)
check("樣本不足的行政區不建立自己的中位數", "北投區" not in b2, str(list(b2.keys())))
fallback = ui_shared.annotate_market(house(0, "10坪", "北投區中央北路", 50000), b2)
check("樣本不足時退回全體中位數", fallback["scope"] == "全部區域", str(fallback))

# 已下架不列入行情
with_off = daan + [house(0, "10坪", "大安區忠孝東路四段", 999000, status="off_market")]
check("已下架房源不影響行情中位數",
      ui_shared.build_market_baseline(with_off).get("大安區") == 2400)

check("坪數為 0 時不標註行情",
      ui_shared.annotate_market(house(0, "未標示坪數", "大安區忠孝東路四段", 24000), baseline) is None)

# ══════════════ 降價歷史 ══════════════
print("\n── 降價歷史 ──")
h_drop = house(23000, "10坪", "大安區", 25000, history=[
    {"price": "26,000元/月", "numeric": 26000, "time": "2026-07-01 10:00:00"},
    {"price": "24,500元/月", "numeric": 24500, "time": "2026-07-15 10:00:00"},
    {"price": "23,000元/月", "numeric": 23000, "time": "2026-07-28 10:00:00"},
])
d = ui_shared.annotate_price_drop(h_drop)
check("總降幅正確", d["total_drop"] == 3000, str(d))
check("降幅百分比正確", d["drop_pct"] == 11.5, str(d["drop_pct"]))
check("調整次數正確", d["change_count"] == 2, str(d["change_count"]))
check("記錄最近調整時間", d["last_change_time"].startswith("2026-07-28"))

check("只有一筆歷史時視為未降價",
      ui_shared.annotate_price_drop(house(23000, "10坪", "大安區", 25000, history=[
          {"numeric": 23000, "time": "2026-07-01 10:00:00"}])) is None)
check("漲價不算降價",
      ui_shared.annotate_price_drop(house(26000, "10坪", "大安區", 28000, history=[
          {"numeric": 23000, "time": "2026-07-01 10:00:00"},
          {"numeric": 26000, "time": "2026-07-20 10:00:00"}])) is None)
check("沒有 price_history 時回傳 None", ui_shared.annotate_price_drop(house(23000, "10坪", "大安區", 25000)) is None)
check("price_history 是壞掉的 JSON 也不會爆",
      ui_shared.annotate_price_drop({"price_history": "{{壞掉"}) is None)

print("\n" + ("新功能測試全部通過 ✅" if not failures else f"失敗 {len(failures)} 項 ❌: {failures}"))
sys.exit(1 if failures else 0)
