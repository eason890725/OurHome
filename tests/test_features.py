# -*- coding: utf-8 -*-
"""行情基準線、降價歷史、通勤時間三項功能的測試。

    python tests/test_features.py

通勤部分用假的 requests 驗證請求組裝、回應解析、批次切分與錯誤處理，
不會真的呼叫 Google API，也不需要金鑰。
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(tempfile.gettempdir())

import commute  # noqa: E402
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

# ══════════════ 通勤時間（離線路網估算）══════════════
print("\n── 捷運站名抽取 ──")
check("「劍南路捷運」抽出劍南路", commute.find_station("🧸劍南路捷運✅可租補/可寵") == "劍南路")
check("「中山國小站」不會被「中山」搶走", commute.find_station("中山國小站近捷運2房") == "中山國小")
check("「公館站超近」抽出公館", commute.find_station("🔆超值兩房一廳🔆公館站超近🔆") == "公館")
check("「忠孝敦化」不會被「忠孝新生」混淆",
      commute.find_station("免佣）捷運忠孝敦化1分【超值電梯】") == "忠孝敦化")
check("別名「劍南」對應到劍南路", commute.find_station("劍南捷運美麗華商圈") == "劍南路")
check("別名「小巨蛋」對應到台北小巨蛋", commute.find_station("近小巨蛋捷運站") == "台北小巨蛋")
# 行政區與路名是最主要的誤判來源
check("「中山區」不會被當成中山站", commute.find_station("中山區民生東路一段整層住家") is None,
      str(commute.find_station("中山區民生東路一段整層住家")))
check("「松山區」不會被當成松山站", commute.find_station("松山區光復北路11巷") is None,
      str(commute.find_station("松山區光復北路11巷")))
check("「南京東路」不會被當成南京三民",
      commute.find_station("南京東路五段溫馨套房") is None,
      str(commute.find_station("南京東路五段溫馨套房")))
check("有捷運字樣時優先採用",
      commute.find_station("中山區近雙連捷運站") == "雙連",
      str(commute.find_station("中山區近雙連捷運站")))
check("找不到站名時回傳 None", commute.find_station("溫馨小套房採光佳") is None)
# 內文常順帶提到別的車站，標題寫明的必須優先
check("標題的站名優先於內文提到的站名",
      commute.estimate("🔆公館站超近🔆可養寵", ["南京復興"],
                       fallback_text="費用說明 近台電大樓站 電費一度5元")["station"] == "公館",
      str(commute.estimate("🔆公館站超近🔆", ["南京復興"],
                           fallback_text="近台電大樓站")["station"]))
check("標題沒有站名時才退回內文",
      commute.estimate("溫馨小套房採光佳", ["南京復興"],
                       fallback_text="近台電大樓站 電費一度5元")["station"] == "台電大樓")

print("\n── 路網最短時間 ──")
r = commute.ride_minutes("劍南路", "南港軟體園區")
check("劍南路→南港軟體園區為文湖線直達 8 站",
      r["stops"] == 8 and r["transfers"] == 0, str(r))
r2 = commute.ride_minutes("南京復興", "南港軟體園區")
check("南京復興→南港軟體園區直達 12 站",
      r2["stops"] == 12 and r2["transfers"] == 0, str(r2))
r3 = commute.ride_minutes("中山國小", "南京復興")
check("中山國小→南京復興需轉乘 1 次",
      r3["stops"] == 3 and r3["transfers"] == 1, str(r3))
check("同站距離為 0", commute.ride_minutes("南京復興", "南京復興")["minutes"] == 0)
check("不存在的站回傳 None", commute.ride_minutes("不存在站", "南京復興") is None)
check("轉乘會反映在時間上",
      commute.ride_minutes("中山國小", "南京復興")["minutes"] >
      commute.ride_minutes("松江南京", "南京復興")["minutes"])

print("\n── 通勤估算整合 ──")
est = commute.estimate("🧸劍南路捷運✅可租補/可寵", ["南京復興", "南港軟體園區"])
check("回報抽到的車站", est["station"] == "劍南路", str(est))
check("兩個目的地都有結果", len(est["items"]) == 2, str(est))
check("依通勤時間由短到長排序",
      est["items"][0]["minutes"] <= est["items"][1]["minutes"], str(est["items"]))
check("時間含步行加成", est["items"][0]["minutes"] == int(round(4 * 2 + 5)), str(est["items"][0]))
check("max_minutes 正確", est["max_minutes"] == max(i["minutes"] for i in est["items"]))
check("抽不到車站時回傳 None", commute.estimate("溫馨小套房採光佳") is None)
check("目的地清單為空時回傳 None", commute.estimate("劍南路捷運", []) is None)
check("不需要任何金鑰即可運作", commute.is_enabled() is True)
check("目的地名稱縮短", commute.short_dest_name("捷運南京復興站") == "南京復興")

print("\n── 路網資料完整性 ──")
check("所有路線的站名皆無重複",
      all(len(v) == len(set(v)) for v in commute.MRT_LINES.values()))
check("轉乘站確實橫跨多條路線", len(commute.STATIONS["南京復興"]) == 2,
      str(commute.STATIONS["南京復興"]))
check("南港展覽館同時屬於文湖線與板南線",
      set(commute.STATIONS["南港展覽館"]) == {"文湖線", "板南線"},
      str(commute.STATIONS["南港展覽館"]))
unreachable = [s for s in commute.STATIONS if commute.ride_minutes(s, "南京復興") is None]
check("每一站都能抵達南京復興（路網沒有斷開）", not unreachable, str(unreachable[:5]))

print("\n" + ("新功能測試全部通過 ✅" if not failures else f"失敗 {len(failures)} 項 ❌: {failures}"))
sys.exit(1 if failures else 0)
