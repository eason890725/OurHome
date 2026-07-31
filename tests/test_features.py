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

# ══════════════ 通勤時間 ══════════════
print("\n── 通勤時間 ──")
check("未設金鑰時功能關閉", commute.is_enabled() is False)
check("沒有金鑰就不發請求", commute.CommuteCalculator(api_key="").get_durations(["大安區"]) == {})

check("地址自動補上台北市", commute.normalize_origin("中山區民生東路一段") == "台北市中山區民生東路一段")
check("新北市行政區補新北市", commute.normalize_origin("板橋區文化路一段") == "新北市板橋區文化路一段")
check("已有縣市則不重複補", commute.normalize_origin("台北市大安區") == "台北市大安區")
check("目的地名稱縮短", commute.short_dest_name("捷運南京復興站") == "南京復興")
check("目的地名稱縮短(軟體園區)", commute.short_dest_name("捷運軟體園區站") == "軟體園區")


class FakeResp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._p


class FakeRequests:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(kw.get("params", {}))
        return FakeResp(self.payload, self.status)


DESTS = ["捷運南京復興站", "捷運軟體園區站"]
ok_payload = {
    "status": "OK",
    "rows": [
        {"elements": [{"status": "OK", "duration": {"value": 1380}},
                      {"status": "OK", "duration": {"value": 2100}}]},
        {"elements": [{"status": "OK", "duration": {"value": 900}},
                      {"status": "ZERO_RESULTS"}]},
    ],
}
fake = FakeRequests(ok_payload)
commute.requests = fake
calc = commute.CommuteCalculator(api_key="fake-key", destinations=DESTS)
res = calc.get_durations(["中山區民生東路一段", "內湖區內湖路一段"])

check("秒數正確換算成分鐘", res["中山區民生東路一段"]["捷運南京復興站"] == 23, str(res))
check("第二個目的地也解析出來", res["中山區民生東路一段"]["捷運軟體園區站"] == 35, str(res))
check("ZERO_RESULTS 的目的地被略過",
      "捷運軟體園區站" not in res.get("內湖區內湖路一段", {}), str(res))
p = fake.calls[0]
check("查詢模式為大眾運輸", p.get("mode") == "transit", str(p.get("mode")))
check("起點有做過正規化", p.get("origins", "").startswith("台北市中山區"), str(p.get("origins")))
check("目的地正確帶入", p.get("destinations") == "捷運南京復興站|捷運軟體園區站")

# 批次切分：60 個起點應該切成 3 次請求（每次上限 25）
fake2 = FakeRequests({"status": "OK", "rows": [{"elements": []}] * 25})
commute.requests = fake2
commute.CommuteCalculator(api_key="k", destinations=DESTS).get_durations([f"大安區路{i}" for i in range(60)])
check("60 個起點切成 3 次請求", len(fake2.calls) == 3, f"{len(fake2.calls)} 次")

# 錯誤處理
commute.requests = FakeRequests({"status": "REQUEST_DENIED", "error_message": "billing not enabled"})
check("API 回應錯誤時回傳空結果並且不拋例外",
      commute.CommuteCalculator(api_key="k", destinations=DESTS).get_durations(["大安區"]) == {})
commute.requests = FakeRequests({}, status=500)
check("HTTP 500 時回傳空結果",
      commute.CommuteCalculator(api_key="k", destinations=DESTS).get_durations(["大安區"]) == {})


class BoomRequests:
    def get(self, *a, **kw):
        raise ConnectionError("模擬斷線")


commute.requests = BoomRequests()
check("網路異常時回傳空結果",
      commute.CommuteCalculator(api_key="k", destinations=DESTS).get_durations(["大安區"]) == {})

# summarize
s = commute.summarize({"捷運南京復興站": 23, "捷運軟體園區站": 35})
check("summarize 依時間由短到長排序", [i["dest"] for i in s["items"]] == ["南京復興", "軟體園區"], str(s))
check("summarize 取出最長通勤時間", s["max_minutes"] == 35)
check("summarize 對空值回傳 None", commute.summarize(None) is None)

print("\n" + ("新功能測試全部通過 ✅" if not failures else f"失敗 {len(failures)} 項 ❌: {failures}"))
sys.exit(1 if failures else 0)
