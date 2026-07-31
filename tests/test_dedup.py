# -*- coding: utf-8 -*-
"""去重演算法測試：案例全部取自 rentals_backup.json 的真實資料。

    python tests/test_dedup.py

重點在「該抓的要抓到，不該抓的絕對不能抓」——誤判成重複的物件會被直接丟棄不入庫，
比漏抓更難察覺。
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 必須在 import db 之前設定：db 模組在 import 當下就會讀取 GITHUB_REPO，
# 設成空字串才會進入純本機模式，測試才不會去下載雲端的真實資料。
os.environ["GITHUB_REPO"] = ""
os.environ.pop("GITHUB_TOKEN", None)

BOX = os.path.join(tempfile.gettempdir(), "ourhome_dedup_test")
shutil.rmtree(BOX, ignore_errors=True)
os.makedirs(BOX)
os.chdir(BOX)

from db import HousingDB, address_verdict, parse_address  # noqa: E402

failures = []


def check(label, cond, extra=""):
    print(("[OK]   " if cond else "[FAIL] ") + label + (f"  {extra}" if extra else ""))
    if not cond:
        failures.append(label)


def h(title, price, size, address):
    return {"title": title, "numeric_price": price, "size": size, "address": address}


db = HousingDB.__new__(HousingDB)  # 只測純函式，不需要真的開資料庫

# ── 地址解析 ──
print("── 地址結構化解析 ──")
p = parse_address("中山區中山北路二段77巷29號")
check("解析出行政區", p and p["district"] == "中山區", str(p))
check("解析出路名", p and p["road"] == "中山北路", str(p))
check("中文數字段轉成數字", p and p["section"] == 2, str(p))
check("解析出巷與號", p and p["lane"] == 77 and p["number"] == 29, str(p))
check("阿拉伯數字段也吃得下", (parse_address("中正區重慶南路3段117號") or {}).get("section") == 3)
check("全形數字正規化", (parse_address("中山區錦州街１１０號") or {}).get("number") == 110)
check("粒度太粗回傳 None（只有行政區）", parse_address("台北市中山區") is None)
check("未提供地址回傳 None", parse_address("未提供地址") is None)

# ── 地址判定 ──
print("\n── 地址 conflict / compatible 判定 ──")
check("不同段 → conflict",
      address_verdict("中山區新生北路三段1號", "中山區新生北路二段") == "conflict")
check("不同路 → conflict",
      address_verdict("中山區松江路16巷", "中山區長安東路二段") == "conflict")
check("不同號 → conflict",
      address_verdict("中山區錦州街110號", "中山區錦州街250號") == "conflict")
check("一邊到門牌一邊到路段 → compatible",
      address_verdict("內湖區內湖路一段49號", "內湖區內湖路一段") == "compatible")
check("巷 vs 號（591 常見混寫）→ compatible",
      address_verdict("大安區敦化南路一段177巷", "大安區敦化南路一段177號") == "compatible")
check("粒度太粗 → unknown",
      address_verdict("台北市中山區", "中山區南京西路") == "unknown")

# ── 真實資料：應判為重複 ──
print("\n── 應判定為重複（取自實際資料）──")
real_dups = [
    ("內湖路一段：門牌 vs 路段，價與坪完全相同",
     h("劍南捷運美麗華商圈雅緻電梯大套房可租補", 24000, "8.2坪", "內湖區內湖路一段49號"),
     h("🧸劍南路捷運✅可租補/可寵/獨洗🔥垃圾代收/電梯美宅", 24000, "8.2坪", "內湖區內湖路一段")),
    ("敦化南路一段：177巷 vs 177號，價差 10 元",
     h("忠孝敦化2分🌻電梯租案／獨立洗衣／可寵物／垃圾代收／管理員", 28990, "7坪", "大安區敦化南路一段177巷"),
     h("免佣）捷運忠孝敦化1分【超值電梯】養寵物★垃圾代收★東區商圈", 29000, "7坪", "大安區敦化南路一段177號")),
    ("重慶南路三段：路段 vs 門牌，坪差 0.2",
     h("稀有陽台華廈/台水台電/租社籍（未稅）", 26000, "11.7坪", "中正區重慶南路三段"),
     h("⭐可租補/可入籍/屋況佳/天然瓦斯/採光好通風佳", 26000, "11.9坪", "中正區重慶南路三段117號")),
    ("松江路16巷：有無門牌，價差 1 元",
     h("松江路長安東路獨立套房", 16000, "12坪", "中山區松江路16巷5號"),
     h("忠孝新生🚇租屋補助‼️變頻冷氣❄️採光通風☀️天然瓦斯🔥", 15999, "12坪", "中山區松江路16巷")),
    ("中山北路二段77巷：原始資料把「巷」打成「巷巷」",
     h("🚇雙連💦乾濕分離⚡️台水台電🗑️垃圾代收7/20可看", 26500, "11.6坪", "中山區中山北路二段77巷"),
     h("🏨華泰飯店旁❤️絕美電梯獨套7/20後可看", 26498, "11.6坪", "中山區中山北路二段77巷巷")),
]
for label, a, b in real_dups:
    check(label, db.is_precise_duplicate(a, b) is True)

# ── 真實資料：絕不可判為重複 ──
print("\n── 絕不可判定為重複（誤判會導致物件被丟棄）──")
real_distinct = [
    ("同地址同棟大樓的不同戶（價差 11,000）",
     h("中山國小捷運全新一房一廳套房可寵車位另計", 25999, "11.4坪", "中山區新生北路三段1號"),
     h("捷運陽台採光房/可寵/可租補/車位另計", 15000, "10坪", "中山區新生北路三段1號")),
    ("新生北路三段 vs 二段（不同路段）",
     h("中山國小捷運全新一房一廳套房可寵車位另計", 25999, "11.4坪", "中山區新生北路三段1號"),
     h("💓中山國小捷運/樓中樓🏡可養貓🐱台電/短租可談", 27500, "11.7坪", "中山區新生北路二段")),
    ("同一條路的不同棟（二段 vs 二段59巷，價差 1,500）",
     h("⭐民權西路捷運⭐紐約紐約鋼骨電梯一房一廳~有廚房", 29500, "11.7坪", "中山區中山北路二段"),
     h("中山舒適美套房", 28000, "11.5坪", "中山區中山北路二段59巷")),
    ("民生東路一段同路段但價差 10,000",
     h("🐣雙連電梯樓中樓/獨立門牌/雙連站5分鐘🐣", 30000, "9坪", "中山區民生東路一段"),
     h("住商林小姐🍎高樓面公園可租補大套房", 20000, "8.7坪", "中山區民生東路一段")),
]
for label, a, b in real_distinct:
    check(label, db.is_precise_duplicate(a, b) is False)

# ── 租金幾乎一致時，坪數容忍度放寬 ──
# 同一間房被不同仲介刊登時坪數常對不起來（權狀坪 vs 室內坪）。
# 使用者確認下列三筆是同一間房，實際刊登坪數為 9 / 8.2 / 8.2。
print("\n── 同價不同坪數（使用者確認為同一間房）──")
check("價差 0、坪差 0.8 判定為重複",
      db.is_precise_duplicate(
          h("超值大套房、可雙租補、垃圾代收、可貓、劍南路站", 24000, "9坪", "內湖區內湖路一段"),
          h("劍南捷運美麗華商圈雅緻電梯大套房可租補", 24000, "8.2坪", "內湖區內湖路一段49號")) is True)
check("價差 0、坪差 1.5 仍判定為重複",
      db.is_precise_duplicate(
          h("A房", 28000, "12坪", "中正區杭州南路一段143巷"),
          h("B房", 28000, "13.5坪", "中正區杭州南路一段")) is True)
check("價差 0、但坪差 2.0 超出容忍範圍",
      db.is_precise_duplicate(
          h("A房", 24000, "9坪", "內湖區內湖路一段"),
          h("B房", 24000, "11坪", "內湖區內湖路一段")) is False)
# 放寬只適用於「租金幾乎完全一致」，價差稍大時仍維持嚴格門檻
check("價差 400 時坪差 0.8 不算重複（僅價格幾乎相同才放寬）",
      db.is_precise_duplicate(
          h("A房", 24000, "9坪", "內湖區內湖路一段"),
          h("B房", 24400, "8.2坪", "內湖區內湖路一段")) is False)

# ── 舊有的標題相似度路徑不能壞掉 ──
print("\n── 標題相似度路徑（回歸）──")
check("標題幾乎相同仍判為重複",
      db.is_precise_duplicate(
          h("大安區溫馨獨立套房近捷運", 20000, "10坪", "台北市大安區"),
          h("大安區溫馨獨立套房近捷運站", 20000, "10坪", "台北市大安區")) is True)
check("標題不像就不判為重複",
      db.is_precise_duplicate(
          h("大安區溫馨獨立套房近捷運", 20000, "10坪", "台北市大安區"),
          h("內湖科學園區全新兩房一廳", 20000, "10坪", "台北市內湖區")) is False)

# ── 標記而非丟棄：重複刊登仍要入庫，且收合時不可弄丟評分 ──
print("\n── 重複刊登以「標記」處理 ──")
import ui_shared  # noqa: E402

live = HousingDB(os.path.join(BOX, "mark.db"))
primary = {"house_id": "P1", "title": "劍南捷運美麗華商圈雅緻電梯大套房可租補",
           "price": "24,000元/月", "address": "內湖區內湖路一段49號", "size": "8.2坪", "status": "active"}
dup = {"house_id": "D1", "title": "超值大套房、可雙租補、垃圾代收、可貓、劍南路站",
       "price": "24,000元/月", "address": "內湖區內湖路一段", "size": "9坪", "status": "active"}

r1 = live.process_house(primary)
r2 = live.process_house(dup)
check("主物件視為全新物件", r1["action"] == "NEW", r1["action"])
check("重複刊登回報 DUPLICATE（不發 Discord 通知）", r2["action"] == "DUPLICATE", r2["action"])

rows = {r["house_id"]: r for r in live.get_all_houses()}
check("重複刊登仍然有入庫（不是被丟棄）", "D1" in rows)
check("重複刊登指向主物件", rows["D1"]["duplicate_of"] == "P1", str(rows["D1"]["duplicate_of"]))
check("主物件本身不是重複", rows["P1"]["duplicate_of"] is None)

# 使用者對「被收合的那筆」評分後，評分不可從畫面上消失
live.update_house_rating("D1", "dislike", sync_git=False)
collapsed = ui_shared.collapse_duplicates(live.get_all_houses())
check("列表只剩主物件", len(collapsed) == 1, f"{len(collapsed)} 筆")
check("重複刊登掛在主物件底下", len(collapsed[0]["duplicates"]) == 1)
check("收合的那筆有帶著評分",
      collapsed[0]["duplicates"][0]["user_rating"] == "dislike",
      str(collapsed[0]["duplicates"][0].get("user_rating")))

# 回頭掃描既有資料：演算法調整前就入庫的重複也要能被標記
with live._get_connection() as c:
    c.execute("""INSERT INTO houses (house_id,title,price,numeric_price,address,size,status,created_at)
                 VALUES ('D2','獨立陽台~可養貓貓房~申請雙租屋補助','24,000元/月',24000,
                         '內湖區內湖路一段','8.2坪','active','2030-01-01 00:00:00')""")
    c.commit()
check("直接寫入的重複尚未被標記",
      {r["house_id"]: r for r in live.get_all_houses()}["D2"]["duplicate_of"] is None)
marked = live.dedupe_existing()
check("dedupe_existing 有標記到它", marked >= 1, f"{marked} 筆")
check("D2 已指向主物件",
      {r["house_id"]: r for r in live.get_all_houses()}["D2"]["duplicate_of"] == "P1")
check("重複執行不會重複標記", live.dedupe_existing() == 0)

print("\n" + ("去重測試全部通過 ✅" if not failures else f"失敗 {len(failures)} 項 ❌: {failures}"))
sys.exit(1 if failures else 0)
