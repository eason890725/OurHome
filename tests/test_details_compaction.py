# -*- coding: utf-8 -*-
"""details_text 壓縮測試。

    python tests/test_details_compaction.py

守護的是實際事故：擷取內頁費用資訊時以「整行」為單位，
但 591 的 HTML 是壓縮過的，整份文件可能只有幾行——只要某一行含費用關鍵字，
那一行就是整頁。結果每筆 details_text 平均 43,760 字元、備份檔膨脹到 17MB，
而這個欄位每次儀表板重建都會被載入並跑 regex，是記憶體一路長到 OOM 的主因。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["GITHUB_REPO"] = ""

from scraper import compact_details_text, DETAILS_MAX_CHARS  # noqa: E402
from cost_calculator import parse_rental_costs  # noqa: E402

failures = []


def check(label, cond, extra=""):
    print(("[OK]   " if cond else "[FAIL] ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        failures.append(label)


print("── 長度控制 ──")
huge = "無關內容 " * 5000 + "管理費 800 元 電費一度 5 元 " + "更多無關內容 " * 5000
out = compact_details_text(huge)
check("超長輸入會被壓縮", len(out) <= DETAILS_MAX_CHARS, f"{len(huge)} -> {len(out)} 字元")
check("壓縮後仍保留管理費", "管理費" in out and "800" in out, out[:80])
check("壓縮後仍保留電費", "電費" in out or "一度" in out)

# 壓縮過的 HTML（整頁只有一行）也必須被限制住
one_line = "<html><body>" + "冗長內容" * 8000 + " 管理費 1200 元/月 " + "冗長內容" * 8000 + "</body></html>"
check("單行的壓縮 HTML 也會被限制", len(compact_details_text(one_line)) <= DETAILS_MAX_CHARS,
      f"{len(one_line)} -> {len(compact_details_text(one_line))}")

print("\n── 雜訊排除 ──")
with_script = ('<script type="application/ld+json">{"@context":"https://schema.org",'
               '"name":"' + "x" * 3000 + '"}</script> 管理費 500 元')
out = compact_details_text(with_script)
check("JSON-LD 區塊被移除", "@context" not in out and "schema.org" not in out, out[:60])
check("script 之外的費用資訊保留", "管理費" in out)

# 頁面內嵌的捷運站座標曾讓「台電大樓站」被誤判成「依台電計費」
coord = '國父紀念館站",25.0413782,121.5575216,1818,"台電大樓站",25.0202411,121.5290642 '
out = compact_details_text(coord + " 管理費 700 元")
check("含座標的片段被丟棄（避免台電大樓站誤判）", "台電大樓站" not in out, out[:70])
check("同一段文字裡真正的費用資訊仍保留", "管理費" in out)

cost_from_coord = parse_rental_costs(out, "20,000元/月")
check("因此不會被誤判為台電計費", cost_from_coord["is_taipower"] is False,
      cost_from_coord["electricity_desc"])

print("\n── 各類費用關鍵字都要保留 ──")
cases = [
    ("台電", "獨立門戶信箱 台水台電 儲熱型電熱水器"),
    ("台水", "獨立門戶信箱 台水台電 儲熱型電熱水器"),
    ("含水", "20,000 元/月 (租金含水費/網路/第四臺)"),
    ("額外費用", "(額外費用 1,200元/月)"),
    ("管理費", "含管理費寬頻挑高3.6米"),
    ("一度", "電費一度 5 元計費"),
    ("垃圾", "垃圾代收 清潔費 200 元"),
]
for kw, snippet in cases:
    padded = "無關文字 " * 2000 + snippet + " 無關文字 " * 2000
    check(f"保留「{kw}」相關片段", kw in compact_details_text(padded),
          compact_details_text(padded)[:50])

print("\n── 費用解析結果不受影響 ──")
real = ("★捷運中山國小★電梯一房廳★台電計費★代收包裹垃圾 優選好屋 近捷運 獨立套房 10坪 "
        "中山區-吉林路26巷 距中山國小 406公尺 29,000 元/月 (額外費用 1,200元/月) "
        + "頁面雜訊" * 4000)
c_full = parse_rental_costs(real, "29,000元/月")
c_compact = parse_rental_costs(compact_details_text(real), "29,000元/月")
check("台電計費判定一致", c_full["is_taipower"] == c_compact["is_taipower"] is True)
check("管理費一致", c_full["management_fee"] == c_compact["management_fee"] == 1200,
      f"{c_full['management_fee']} vs {c_compact['management_fee']}")

print("\n── 邊界情況 ──")
check("空字串不會出錯", compact_details_text("") == "")
check("None 不會出錯", compact_details_text(None) == "")
check("沒有費用關鍵字時仍回傳截斷後的文字",
      len(compact_details_text("純粹描述" * 5000)) <= DETAILS_MAX_CHARS)
check("短文字原樣保留", "管理費 500 元" in compact_details_text("管理費 500 元"))

print("\n" + ("details_text 壓縮測試全部通過 ✅" if not failures else f"失敗 {len(failures)} 項 ❌: {failures}"))
sys.exit(1 if failures else 0)
