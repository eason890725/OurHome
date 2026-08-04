# -*- coding: utf-8 -*-
"""app.py (Flask 雲端) 與 dashboard.py (本地 http.server) 共用的前端模板與資料格式化邏輯。

抽出原因：兩個入口原本各自維護一份約 600 行、僅 <title> 不同的 HTML_TEMPLATE，
以及一份完全相同的 get_formatted_houses_cached()，改一邊漏一邊會讓本地與雲端行為分岔。
修改儀表板 UI 或卡片欄位時，只需要改這個檔案。
"""
import re
import json
import time
import hashlib
import logging
import statistics
from typing import Any, Dict, List, Optional

from cost_calculator import parse_rental_costs
from text_features import detect_couples_warnings, detect_couples_features

logger = logging.getLogger(__name__)

# 行情基準線：某行政區至少要有這麼多筆在架房源，中位數才有代表性
MARKET_MIN_SAMPLE = 5

# 相對中位數的偏離幅度分級（單位：百分比）
MARKET_LEVELS = [
    (-15, "cheap", "🟢 明顯低於行情"),
    (-5, "below", "🟢 略低於行情"),
    (5, "fair", "⚪ 行情價"),
    (15, "above", "🟠 略高於行情"),
]
MARKET_LEVEL_TOP = ("expensive", "🔴 明顯高於行情")


def parse_sqft(size_str) -> float:
    m = re.search(r'(\d+(?:\.\d+)?)', str(size_str or ""))
    return float(m.group(1)) if m else 0.0


def extract_district(address: str) -> str:
    """從地址取出行政區。

    必須先剝掉縣市，否則「台北市中山區」會被貪婪地比對成「市中山區」，
    導致同一個行政區在有無縣市前綴時被當成兩區，中位數也跟著失真。
    """
    addr = re.sub(r'^[一-龥]{2,3}[市縣]', '', (address or "").strip())
    m = re.search(r'([一-龥]{1,3}[區鄉鎮])', addr)
    return m.group(1) if m else ""


def build_market_baseline(houses: List[Dict[str, Any]]) -> Dict[str, float]:
    """算出各行政區「每坪真實月成本」的中位數，以及全體的中位數（鍵為空字串）。

    只採計在架房源——已下架的價格可能已經過時，不能代表現在的行情。
    """
    by_district: Dict[str, List[float]] = {}
    overall: List[float] = []

    for h in houses:
        if h.get("status") == "off_market":
            continue
        sqft = parse_sqft(h.get("size"))
        total = (h.get("cost_info") or {}).get("total_estimated_cost") or 0
        if sqft <= 0 or total <= 0:
            continue
        unit = total / sqft
        overall.append(unit)
        d = extract_district(h.get("address", ""))
        if d:
            by_district.setdefault(d, []).append(unit)

    baseline: Dict[str, float] = {}
    if overall:
        baseline[""] = statistics.median(overall)
    for d, vals in by_district.items():
        if len(vals) >= MARKET_MIN_SAMPLE:
            baseline[d] = statistics.median(vals)
    return baseline


def annotate_market(house: Dict[str, Any], baseline: Dict[str, float]) -> Optional[Dict[str, Any]]:
    """標註這筆房源相對於同區行情的位置。資料不足就回傳 None（前端不顯示）。"""
    sqft = parse_sqft(house.get("size"))
    total = (house.get("cost_info") or {}).get("total_estimated_cost") or 0
    if sqft <= 0 or total <= 0 or not baseline:
        return None

    district = extract_district(house.get("address", ""))
    median = baseline.get(district)
    scope = district
    if median is None:
        median = baseline.get("")
        scope = "全部區域"
    if not median:
        return None

    unit = total / sqft
    diff_pct = (unit - median) / median * 100

    level, label = MARKET_LEVEL_TOP
    for threshold, lv, lb in MARKET_LEVELS:
        if diff_pct < threshold:
            level, label = lv, lb
            break

    return {
        "scope": scope,
        "unit_cost": round(unit),
        "median_unit_cost": round(median),
        "diff_pct": round(diff_pct),
        "level": level,
        "label": label,
    }


def collapse_duplicates(houses: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把標記為重複刊登的房源收進主物件底下，只回傳主物件。

    重複的那幾筆不會被刪掉，而是掛在主物件的 `duplicates` 欄位裡，
    儀表板上顯示成「🔁 另有 N 筆相同刊登」並可展開，誤判時看得到也點得進去。
    """
    by_id = {str(h.get("house_id")): h for h in houses}
    primaries: List[Dict[str, Any]] = []

    for h in houses:
        h.setdefault("duplicates", [])

    for h in houses:
        # 命中排除關鍵字的直接不進列表（仍留在資料庫，關鍵字拿掉就會回來）
        if h.get("excluded_by"):
            continue
        dup_of = h.get("duplicate_of")
        parent = by_id.get(str(dup_of)) if dup_of else None
        if parent is not None and parent is not h:
            parent.setdefault("duplicates", []).append({
                "house_id": h.get("house_id"),
                "title": h.get("title"),
                "price": h.get("price"),
                "size": h.get("size"),
                "address": h.get("address"),
                "link": h.get("link"),
                "status": h.get("status"),
                # 評分一定要帶上來，否則使用者標過的紀錄會隨著收合一起消失
                "user_rating": h.get("user_rating") or "none",
            })
        else:
            # 主物件；主物件不存在（例如已被清掉）時，這筆自己升為主物件
            primaries.append(h)

    return primaries


def annotate_price_drop(house: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """從 price_history 整理出降價資訊。沒降過價就回傳 None。

    price_history 一直都有在寫入，但過去前端完全沒有拿來顯示。
    """
    raw = house.get("price_history")
    if not raw:
        return None
    try:
        history = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None
    if not isinstance(history, list) or len(history) < 2:
        return None

    nums = [h.get("numeric") for h in history if isinstance(h.get("numeric"), (int, float)) and h.get("numeric") > 0]
    if len(nums) < 2:
        return None

    first, current = nums[0], nums[-1]
    if current >= first:
        return None

    return {
        "original_price": first,
        "current_price": current,
        "total_drop": first - current,
        "drop_pct": round((first - current) / first * 100, 1),
        "change_count": len(nums) - 1,
        "last_change_time": history[-1].get("time", ""),
        "history": [{"price": h.get("numeric"), "time": h.get("time", "")}
                    for h in history if isinstance(h.get("numeric"), (int, float))],
    }

# 儀表板 /api/houses 的記憶體快取存續秒數
CACHE_TTL_SECONDS = 5

# 送給瀏覽器的 details_text 上限。這個欄位只用於前端的自由文字搜尋，
# 完整內容沒有必要傳輸；輪詢頻率高，每一 KB 都會被放大。
API_DETAILS_CHARS = 300

_HOUSES_CACHE = []
_CACHE_LAST_UPDATE = 0


def get_formatted_houses(db, scraper=None):
    """讀取全部房屋並即時附掛 cost_info / 雙人警示 / 雙人配備標籤（帶 5 秒快取）。

    費用與標籤刻意不落 DB，而是每次讀取時依當下 MODE 重新計算，
    因此切換 .env 的 MODE（couple/single）不需要重建資料庫。
    """
    global _HOUSES_CACHE, _CACHE_LAST_UPDATE
    now = time.time()
    if _HOUSES_CACHE and (now - _CACHE_LAST_UPDATE < CACHE_TTL_SECONDS):
        return _HOUSES_CACHE

    houses = db.get_all_houses()
    for h in houses:
        full_text = f"{h.get('title', '')} {h.get('address', '')} {h.get('details_text', '')}"
        h["cost_info"] = parse_rental_costs(full_text, h.get("price", "0"))
        h["couples_warnings"] = detect_couples_warnings(full_text)
        h["couples_features"] = detect_couples_features(full_text)
        h["price_drop"] = annotate_price_drop(h)

    # 行情基準線要先看過全部房源才算得出中位數，因此獨立跑第二輪
    baseline = build_market_baseline(houses)
    for h in houses:
        h["market"] = annotate_market(h, baseline)

    houses = collapse_duplicates(houses)

    _HOUSES_CACHE = houses
    _CACHE_LAST_UPDATE = now
    return _HOUSES_CACHE


def payload_for_api(houses: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """精簡要送給瀏覽器的欄位。

    儀表板會定期輪詢，每一 KB 都會乘上輪詢次數。
    price_history 前端完全沒有使用（降價資訊已由後端算成 price_drop），
    details_text 只用於自由文字搜尋，不需要完整內容。
    """
    slim = []
    for h in houses:
        item = {k: v for k, v in h.items() if k not in ("price_history", "details_text")}
        item["details_text"] = (h.get("details_text") or "")[:API_DETAILS_CHARS]
        slim.append(item)
    return slim


def compute_etag(payload) -> str:
    """依內容產生 ETag，讓瀏覽器在資料沒變時拿到 304。

    資料每 CHECK_INTERVAL_MINUTES 分鐘才變一次，但儀表板輪詢頻率高得多，
    絕大多數請求其實可以只回 304。
    """
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return '"' + hashlib.md5(raw.encode("utf-8")).hexdigest() + '"'


def etag_matches(if_none_match: Optional[str], etag: str) -> bool:
    """比對 If-None-Match 與自己產生的 ETag，兩邊都先正規化。

    不能直接用字串相等：Render 前面的 Cloudflare 會做 gzip 轉換，
    因而把 ETag 改寫成弱驗證器——送出 `"abc"`，實際回到瀏覽器的是 `W/"abc"`，
    瀏覽器再原樣送回來。直接比對就永遠不相等，304 完全不會發生
    （實測線上就是這樣，頻寬修正等於沒生效）。
    也一併處理代理常加的 -gzip 後綴與逗號分隔的多個值。
    """
    if not if_none_match:
        return False
    if if_none_match.strip() == "*":
        return True

    def norm(tag: str) -> str:
        tag = tag.strip()
        if tag.startswith(("W/", "w/")):
            tag = tag[2:]
        tag = tag.strip('"')
        for suffix in ("-gzip", "-br", "-df"):
            if tag.endswith(suffix):
                tag = tag[: -len(suffix)]
        return tag

    target = norm(etag)
    return any(norm(t) == target for t in if_none_match.split(","))


def invalidate_houses_cache():
    """使用者評分等寫入操作後呼叫，讓下一次 /api/houses 立即讀到新狀態。"""
    global _HOUSES_CACHE, _CACHE_LAST_UPDATE
    _HOUSES_CACHE = []
    _CACHE_LAST_UPDATE = 0


DEFAULT_PAGE_TITLE = "OurHome 租屋品質與成本儀表板"

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>__PAGE_TITLE__</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Noto+Sans+TC:wght@400;500;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-dark: #0f172a;
            --card-bg: rgba(30, 41, 59, 0.7);
            --card-border: rgba(255, 255, 255, 0.1);
            --accent-blue: #38bdf8;
            --accent-green: #10b981;
            --accent-yellow: #f59e0b;
            --accent-red: #ef4444;
            --accent-purple: #8b5cf6;
            --accent-gray: #64748b;
            --text-main: #f8fafc;
            --text-sub: #94a3b8;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            font-family: 'Inter', 'Noto Sans TC', sans-serif;
            background-color: var(--bg-dark);
            background-image: 
                radial-gradient(at 0% 0%, rgba(56, 189, 248, 0.12) 0px, transparent 50%),
                radial-gradient(at 100% 100%, rgba(16, 185, 129, 0.1) 0px, transparent 50%);
            background-attachment: fixed;
            color: var(--text-main);
            min-height: 100vh;
            padding: 24px;
        }

        .container { max-width: 1300px; margin: 0 auto; }

        header {
            display: flex; justify-content: space-between; align-items: center;
            margin-bottom: 24px; padding-bottom: 20px; border-bottom: 1px solid var(--card-border);
        }

        .brand h1 {
            font-size: 26px; font-weight: 800;
            background: linear-gradient(135deg, #38bdf8, #818cf8);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
            display: flex; align-items: center; gap: 10px;
        }

        .brand p { color: var(--text-sub); font-size: 14px; margin-top: 4px; }

        .header-actions { display: flex; gap: 10px; align-items: center; }

        .refresh-btn {
            background: linear-gradient(135deg, #0284c7, #2563eb);
            color: white; border: none; padding: 10px 16px; border-radius: 8px;
            font-weight: 600; font-size: 14px; cursor: pointer; transition: all 0.2s ease;
            box-shadow: 0 4px 12px rgba(37, 99, 235, 0.3); display: flex; align-items: center; gap: 6px;
        }

        .refresh-btn:hover { opacity: 0.9; transform: translateY(-1px); }

        .backup-btn {
            background: rgba(255, 255, 255, 0.08); border: 1px solid var(--card-border);
            color: var(--text-main); padding: 10px 14px; border-radius: 8px;
            font-weight: 600; font-size: 13px; cursor: pointer; transition: all 0.2s;
        }
        .backup-btn:hover { background: rgba(255, 255, 255, 0.15); }

        .stats-grid {
            display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
            gap: 16px; margin-bottom: 24px;
        }

        .stat-card {
            background: var(--card-bg); backdrop-filter: blur(12px);
            border: 1px solid var(--card-border); border-radius: 12px; padding: 16px 20px;
        }

        .stat-label { font-size: 13px; color: var(--text-sub); font-weight: 500; }
        .stat-value { font-size: 24px; font-weight: 700; margin-top: 6px; color: var(--text-main); }
        .stat-value.highlight-green { color: var(--accent-green); }
        .stat-value.highlight-yellow { color: var(--accent-yellow); }
        .stat-value.highlight-blue { color: var(--accent-blue); }
        .stat-value.highlight-red { color: #f43f5e; }
        .stat-value.highlight-purple { color: #c084fc; }
        .stat-value.highlight-gray { color: #94a3b8; }

        .controls-card {
            background: var(--card-bg); backdrop-filter: blur(12px);
            border: 1px solid var(--card-border); border-radius: 14px; padding: 20px; margin-bottom: 28px;
        }

        .search-box {
            width: 100%; padding: 12px 16px; border-radius: 10px;
            background: rgba(15, 23, 42, 0.6); border: 1px solid var(--card-border);
            color: var(--text-main); font-size: 15px; outline: none; margin-bottom: 16px; transition: border 0.2s;
        }

        .search-box:focus { border-color: var(--accent-blue); }

        .filters-row { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 12px; }
        .pill-group { display: flex; flex-wrap: wrap; gap: 8px; }

        .pill {
            background: rgba(255, 255, 255, 0.06); border: 1px solid var(--card-border);
            color: var(--text-sub); padding: 7px 14px; border-radius: 20px; font-size: 13px;
            font-weight: 500; cursor: pointer; user-select: none; transition: all 0.2s;
        }

        .pill:hover { background: rgba(255, 255, 255, 0.12); color: var(--text-main); }
        .pill.active { background: #0284c7; color: white; border-color: #38bdf8; font-weight: 600; }
        .pill.pill-unrated.active { background: #8b5cf6; color: white; border-color: #a78bfa; font-weight: 600; box-shadow: 0 2px 8px rgba(139, 92, 246, 0.4); }
        .pill.pill-offmarket.active { background: #475569; color: white; border-color: #94a3b8; font-weight: 600; }
        .pill.pill-like.active { background: #e11d48; color: white; border-color: #fb7185; }
        .pill.pill-neutral.active { background: #d97706; color: white; border-color: #fbbf24; }
        .pill.pill-dislike.active { background: #475569; color: white; border-color: #94a3b8; }

        .sort-select {
            background: rgba(15, 23, 42, 0.6); border: 1px solid var(--card-border);
            color: var(--text-main); padding: 8px 14px; border-radius: 8px; font-size: 13px; outline: none; cursor: pointer;
        }

        .listings-grid {
            display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 20px;
        }

        .house-card {
            background: var(--card-bg); backdrop-filter: blur(12px);
            border: 1px solid var(--card-border); border-radius: 14px; padding: 20px;
            display: flex; flex-direction: column; justify-content: space-between;
            transition: transform 0.2s, border-color 0.2s, box-shadow 0.2s;
            position: relative;
        }

        .house-card.rated-like { border-color: rgba(244, 63, 94, 0.5); background: rgba(30, 41, 59, 0.85); }
        .house-card.rated-dislike { opacity: 0.5; filter: grayscale(0.5); }
        .house-card.rated-none { border-color: rgba(139, 92, 246, 0.3); }
        .house-card.status-off_market { opacity: 0.55; filter: grayscale(0.4); background: rgba(15, 23, 42, 0.7); }

        /* 行情基準線 */
        .badge-market { display:inline-block; padding:4px 10px; border-radius:8px; font-size:0.78rem; font-weight:700; border:1px solid; }
        .market-cheap     { background:rgba(16,185,129,0.18); color:#34d399; border-color:rgba(16,185,129,0.35); }
        .market-below     { background:rgba(16,185,129,0.10); color:#6ee7b7; border-color:rgba(16,185,129,0.25); }
        .market-fair      { background:rgba(148,163,184,0.12); color:#cbd5e1; border-color:rgba(148,163,184,0.25); }
        .market-above     { background:rgba(249,115,22,0.12);  color:#fb923c; border-color:rgba(249,115,22,0.3); }
        .market-expensive { background:rgba(239,68,68,0.15);   color:#f87171; border-color:rgba(239,68,68,0.35); }
        .market-detail { font-size:0.72rem; color:var(--text-dim); margin-top:4px; }

        /* 降價歷史 */
        .badge-drop { display:inline-block; padding:4px 10px; border-radius:8px; font-size:0.78rem; font-weight:700;
                      background:rgba(239,68,68,0.15); color:#f87171; border:1px solid rgba(239,68,68,0.35); }
        .drop-detail { font-size:0.72rem; color:var(--text-dim); margin-top:4px; }
        .drop-detail s { opacity:0.7; }


        /* 重複刊登 */
        .dup-block { margin-top:10px; }
        .dup-toggle { width:100%; text-align:left; cursor:pointer; padding:6px 10px; border-radius:8px;
                      font-size:0.76rem; font-weight:600; font-family:inherit;
                      background:rgba(168,85,247,0.12); color:#c4b5fd;
                      border:1px solid rgba(168,85,247,0.3); }
        .dup-toggle:hover { background:rgba(168,85,247,0.2); }
        .dup-list { margin-top:6px; padding:8px 10px; border-radius:8px; background:rgba(15,23,42,0.5);
                    border:1px solid rgba(148,163,184,0.15); }
        .dup-item { padding:5px 0; border-bottom:1px solid rgba(148,163,184,0.1); font-size:0.74rem; }
        .dup-item:last-child { border-bottom:none; }
        .dup-item a { color:#93c5fd; text-decoration:none; font-weight:600; display:block; }
        .dup-item a:hover { text-decoration:underline; }
        .dup-item span { color:var(--text-dim); font-size:0.7rem; }
        .dup-rating { color:#fbbf24; margin-left:6px; }

        .house-card:hover {
            transform: translateY(-3px); border-color: rgba(56, 189, 248, 0.4);
            box-shadow: 0 10px 24px rgba(0, 0, 0, 0.3);
        }

        .rating-toolbar {
            display: flex; gap: 6px; margin-top: 14px; padding-top: 12px;
            border-top: 1px solid rgba(255, 255, 255, 0.08); align-items: center; justify-content: space-between;
        }

        .rating-btn-group { display: flex; gap: 6px; }

        .rating-btn {
            background: rgba(255, 255, 255, 0.06); border: 1px solid var(--card-border);
            color: var(--text-sub); padding: 6px 12px; border-radius: 8px; font-size: 12px;
            font-weight: 600; cursor: pointer; transition: all 0.2s; display: flex; align-items: center; gap: 4px;
        }

        .rating-btn:hover { background: rgba(255, 255, 255, 0.15); color: var(--text-main); }
        
        .rating-btn.active-like { background: #e11d48; color: white; border-color: #fb7185; box-shadow: 0 2px 8px rgba(225, 29, 72, 0.4); }
        .rating-btn.active-neutral { background: #d97706; color: white; border-color: #fbbf24; box-shadow: 0 2px 8px rgba(217, 119, 6, 0.4); }
        .rating-btn.active-dislike { background: #475569; color: white; border-color: #94a3b8; }

        .badge-taipower {
            display: inline-block; background: rgba(245, 158, 11, 0.15); color: #fbbf24;
            border: 1px solid rgba(245, 158, 11, 0.3); font-size: 12px; font-weight: 700;
            padding: 4px 10px; border-radius: 6px; margin-bottom: 8px;
        }

        .badge-normal {
            display: inline-block; background: rgba(56, 189, 248, 0.15); color: #38bdf8;
            border: 1px solid rgba(56, 189, 248, 0.3); font-size: 12px; font-weight: 600;
            padding: 4px 10px; border-radius: 6px; margin-bottom: 8px;
        }

        .badge-unrated {
            display: inline-block; background: rgba(139, 92, 246, 0.15); color: #c084fc;
            border: 1px solid rgba(139, 92, 246, 0.3); font-size: 12px; font-weight: 600;
            padding: 4px 10px; border-radius: 6px; margin-bottom: 8px;
        }

        .badge-offmarket {
            display: inline-block; background: rgba(148, 163, 184, 0.2); color: #cbd5e1;
            border: 1px solid rgba(148, 163, 184, 0.3); font-size: 12px; font-weight: 600;
            padding: 4px 10px; border-radius: 6px; margin-bottom: 8px;
        }

        .house-title {
            font-size: 17px; font-weight: 700; line-height: 1.4; color: var(--text-main); text-decoration: none; display: block;
        }

        .house-title:hover { color: var(--accent-blue); }

        .meta-pills { display: flex; gap: 8px; margin: 12px 0; flex-wrap: wrap; }
        .meta-tag {
            background: rgba(255, 255, 255, 0.05); border: 1px solid rgba(255, 255, 255, 0.08);
            font-size: 12px; color: var(--text-sub); padding: 4px 8px; border-radius: 6px;
        }

        .cost-block {
            background: rgba(15, 23, 42, 0.6); border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 10px; padding: 14px; margin: 12px 0;
        }

        .cost-title { font-size: 12px; color: var(--text-sub); margin-bottom: 4px; }
        .cost-amount { font-size: 22px; font-weight: 800; color: var(--accent-green); }
        .cost-details {
            margin-top: 8px; padding-top: 8px; border-top: 1px dashed rgba(255, 255, 255, 0.1);
            font-size: 12px; color: var(--text-sub); display: grid; grid-template-columns: 1fr 1fr; gap: 4px;
        }

        .tags-section { margin: 8px 0; display: flex; flex-direction: column; gap: 6px; }
        .tag-warning {
            background: rgba(239, 68, 68, 0.12); color: #fca5a5;
            border: 1px solid rgba(239, 68, 68, 0.25); font-size: 12px; padding: 4px 8px; border-radius: 6px;
        }
        .tag-feature {
            background: rgba(16, 185, 129, 0.12); color: #6ee7b7;
            border: 1px solid rgba(16, 185, 129, 0.25); font-size: 12px; padding: 4px 8px; border-radius: 6px;
            display: inline-block; margin-right: 4px;
        }

        .btn-link {
            display: inline-block; text-align: center; background: rgba(255, 255, 255, 0.08);
            border: 1px solid var(--card-border); color: var(--text-main); padding: 7px 12px;
            border-radius: 8px; font-size: 12px; font-weight: 600; text-decoration: none; transition: all 0.2s;
        }

        .btn-link:hover { background: #0284c7; border-color: #38bdf8; color: white; }
        .no-data { text-align: center; padding: 60px; color: var(--text-sub); font-size: 16px; grid-column: 1 / -1; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="brand">
                <h1 id="pageHeading">🏠 OurHome 租屋品質與成本儀表板</h1>
                <p>雲端 24H 免費零成本評分記憶連動系統</p>
            </div>
            <div class="header-actions">
                <button class="backup-btn" onclick="exportBackup()">📥 匯出紀錄備份</button>
                <button class="refresh-btn" onclick="fetchHouses()">🔄 列車刷新</button>
            </div>
        </header>

        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-label">合格房源總數</div>
                <div class="stat-value highlight-blue" id="stat-total">0 筆</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">❓ 未選擇/未看過</div>
                <div class="stat-value highlight-purple" id="stat-unrated">0 筆</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">❤️ 喜愛精選物件</div>
                <div class="stat-value highlight-red" id="stat-like">0 筆</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">✨ 台電計費神房</div>
                <div class="stat-value highlight-yellow" id="stat-taipower">0 筆</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">🏚️ 已下架/已出租</div>
                <div class="stat-value highlight-gray" id="stat-offmarket">0 筆</div>
            </div>
        </div>

        <div class="controls-card">
            <input type="text" id="searchInput" class="search-box" placeholder="🔍 搜尋框支援同義字自動連動 (輸入『租補』可自動找出『租屋補助/租金補貼/社宅/補助』的所有房源)..." oninput="filterAndRender()">

            <div class="filters-row">
                <div class="pill-group" id="filterPills">
                    <!-- 動態行政區與評價標籤渲染區 -->
                </div>

                <select id="sortSelect" class="sort-select" onchange="filterAndRender()">
                    <option value="cost_asc">排序：預估月總成本 (低 ➔ 高)</option>
                    <option value="rent_asc">排序：刊登租金 (低 ➔ 高)</option>
                    <option value="size_desc">排序：坪數 (大 ➔ 小)</option>
                    <option value="time_desc">排序：最新上架/更新時間</option>
                    <option value="market_asc">排序：相對行情 (便宜 ➔ 貴)</option>
                    <option value="drop_desc">排序：降價幅度 (大 ➔ 小)</option>
                </select>
            </div>
        </div>

        <div class="listings-grid" id="listingsContainer">
            <div class="no-data">正在載入最新全物件列表...</div>
        </div>
    </div>

    <script>
        let allHouses = [];
        let currentFilter = 'all';
        let isFirstLoad = true;

        const SYNONYM_GROUPS = [
            ["租補", "租屋補助", "租金補貼", "補助", "社宅", "補貼", "可補"],
            ["陽台", "獨陽", "獨立陽台", "陽臺"],
            ["洗衣機", "獨洗", "獨立洗衣機", "洗脫"],
            ["台電", "依台電", "台電計費", "台灣電力"]
        ];

        function getLocalRatings() {
            try { return JSON.parse(localStorage.getItem('ourhome_ratings') || '{}'); } catch { return {}; }
        }
        function saveLocalRating(houseId, rating) {
            const ratings = getLocalRatings();
            if (rating === 'none') delete ratings[houseId];
            else ratings[houseId] = rating;
            localStorage.setItem('ourhome_ratings', JSON.stringify(ratings));
        }

        function syncLocalRatingsToServer() {
            const localRatings = getLocalRatings();
            if (Object.keys(localRatings).length > 0) {
                fetch('/api/sync_ratings', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ ratings: localRatings })
                }).catch(e => {});
            }
        }

        async function fetchHouses() {
            try {
                if (isFirstLoad) {
                    syncLocalRatingsToServer();
                    isFirstLoad = false;
                }

                const res = await fetch('/api/houses');
                allHouses = await res.json();
                
                const localRatings = getLocalRatings();
                allHouses.forEach(h => {
                    if (localRatings[h.house_id]) {
                        h.user_rating = localRatings[h.house_id];
                    }
                });

                renderDynamicDistrictPills();
                updateStats();
                filterAndRender();
            } catch (err) {
                console.error("載入房屋列表失敗:", err);
            }
        }

        async function setHouseRating(houseId, newRating) {
            const target = allHouses.find(h => String(h.house_id) === String(houseId));
            if (target) {
                const finalRating = (target.user_rating === newRating) ? 'none' : newRating;
                target.user_rating = finalRating;
                saveLocalRating(houseId, finalRating);
                updateStats();
                filterAndRender();

                try {
                    await fetch('/api/rating', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ house_id: houseId, rating: finalRating })
                    });
                } catch (err) {
                    console.error("儲存評價失敗:", err);
                }
            }
        }

        function exportBackup() {
            const backupData = {
                ratings: getLocalRatings(),
                export_time: new Date().toISOString(),
                houses: allHouses
            };
            const dataStr = "data:text/json;charset=utf-8," + encodeURIComponent(JSON.stringify(backupData, null, 2));
            const downloadAnchor = document.createElement('a');
            downloadAnchor.setAttribute("href", dataStr);
            downloadAnchor.setAttribute("download", `OurHome_Rentals_Backup_${new Date().toISOString().slice(0,10)}.json`);
            document.body.appendChild(downloadAnchor);
            downloadAnchor.click();
            downloadAnchor.remove();
        }

        function renderDynamicDistrictPills() {
            const pillGroup = document.getElementById('filterPills');
            const knownDistricts = [
                "大安區", "中山區", "信義區", "松山區", "南港區", "內湖區", "士林區", "北投區", "萬華區", "中正區", "大同區", "文山區",
                "板橋區", "新莊區", "中和區", "永和區", "三重區", "新店區", "土城區", "蘆洲區", "汐止區", "樹林區", "淡水區", "三峽區", "林口區", "鶯歌區", "五股區", "泰山區", "八里區",
                "東區", "西區", "南區", "北區", "中區", "安平區", "左營區", "鼓山區", "三民區"
            ];

            const presentDistricts = new Set();
            allHouses.forEach(h => {
                const addr = h.address || '';
                for (const d of knownDistricts) {
                    if (addr.includes(d)) presentDistricts.add(d);
                }
            });

            const countRating = (r) => {
                if (r === 'none') {
                    return allHouses.filter(h => (!h.user_rating || h.user_rating === 'none') && h.status !== 'off_market').length;
                }
                return allHouses.filter(h => h.user_rating === r).length;
            };

            const countStatus = (s) => allHouses.filter(h => h.status === s).length;

            const fixedPillsHtml = `
                <div class="pill ${currentFilter === 'all' ? 'active' : ''}" data-filter="all" onclick="setFilter('all', this)">全部房源</div>
                <div class="pill pill-unrated ${currentFilter === 'unrated' ? 'active' : ''}" data-filter="unrated" onclick="setFilter('unrated', this)">❓ 未選擇 (${countRating('none')})</div>
                <div class="pill pill-like ${currentFilter === 'like' ? 'active' : ''}" data-filter="like" onclick="setFilter('like', this)">❤️ 喜歡的房源 (${countRating('like')})</div>
                <div class="pill pill-neutral ${currentFilter === 'neutral' ? 'active' : ''}" data-filter="neutral" onclick="setFilter('neutral', this)">😐 普通紀錄 (${countRating('neutral')})</div>
                <div class="pill pill-dislike ${currentFilter === 'dislike' ? 'active' : ''}" data-filter="dislike" onclick="setFilter('dislike', this)">💔 不喜歡/已淘汰 (${countRating('dislike')})</div>
                <div class="pill pill-offmarket ${currentFilter === 'off_market' ? 'active' : ''}" data-filter="off_market" onclick="setFilter('off_market', this)">🏚️ 已下架/已出租 (${countStatus('off_market')})</div>
                <div class="pill ${currentFilter === 'subsidy' ? 'active' : ''}" data-filter="subsidy" onclick="setFilter('subsidy', this)">📜 可租補</div>
                <div class="pill ${currentFilter === 'taipower' ? 'active' : ''}" data-filter="taipower" onclick="setFilter('taipower', this)">✨ 台電神房</div>
                <div class="pill ${currentFilter === 'balcony' ? 'active' : ''}" data-filter="balcony" onclick="setFilter('balcony', this)">🧺 有獨立陽台</div>
                <div class="pill ${currentFilter === 'washing' ? 'active' : ''}" data-filter="washing" onclick="setFilter('washing', this)">🧺 獨立洗衣機</div>
                <div class="pill ${currentFilter === 'below_market' ? 'active' : ''}" data-filter="below_market" onclick="setFilter('below_market', this)">🟢 低於行情 (${allHouses.filter(h => h.market && (h.market.level === 'cheap' || h.market.level === 'below')).length})</div>
                <div class="pill ${currentFilter === 'dropped' ? 'active' : ''}" data-filter="dropped" onclick="setFilter('dropped', this)">📉 有降價 (${allHouses.filter(h => h.price_drop).length})</div>
            `;

            const districtPillsHtml = Array.from(presentDistricts).sort().map(d => `
                <div class="pill ${currentFilter === d ? 'active' : ''}" data-filter="${d}" onclick="setFilter('${d}', this)">📍 ${d}</div>
            `).join('');

            pillGroup.innerHTML = fixedPillsHtml + districtPillsHtml;
        }

        function updateStats() {
            const total = allHouses.length;
            const unratedCount = allHouses.filter(h => (!h.user_rating || h.user_rating === 'none') && h.status !== 'off_market').length;
            const likeCount = allHouses.filter(h => h.user_rating === 'like').length;
            const taipowerCount = allHouses.filter(h => h.cost_info && h.cost_info.is_taipower).length;
            const offmarketCount = allHouses.filter(h => h.status === 'off_market').length;

            if (allHouses.length > 0 && allHouses[0].cost_info && allHouses[0].cost_info.mode_label) {
                document.getElementById('pageHeading').innerText = `🏠 OurHome ${allHouses[0].cost_info.mode_label}租屋儀表板`;
            }

            document.getElementById('stat-total').innerText = `${total} 筆`;
            document.getElementById('stat-unrated').innerText = `${unratedCount} 筆`;
            document.getElementById('stat-like').innerText = `${likeCount} 筆`;
            document.getElementById('stat-taipower').innerText = `${taipowerCount} 筆`;
            document.getElementById('stat-offmarket').innerText = `${offmarketCount} 筆`;
        }

        function setFilter(filterType, element) {
            currentFilter = filterType;
            document.querySelectorAll('.pill').forEach(p => p.classList.remove('active'));
            element.classList.add('active');
            filterAndRender();
        }

        function cleanAddressDisplay(rawAddr) {
            if (!rawAddr) return '未提供地址';
            let clean = rawAddr.replace(/^(依現場|社區名稱|所屬社區|高樓層|電梯大樓|大廈|無|未知)+/g, '').trim();
            clean = clean.replace(/(整層住家出租|整層住家|獨立套房出租|獨立套房|分租套房|雅房|住家出租|住家|出租)+$/g, '').trim();

            const roadMatch = clean.match(/((?:[\u4e00-\u9fa5]{2,3}[市縣])?[\u4e00-\u9fa5]{2,4}[區市鎮鄉][\\s\\-–—─]*[\u4e00-\u9fa5\\dA-Za-z]+(?:路|街|段|巷|弄|號|大道)?)/);
            if (roadMatch) return roadMatch[1].replace(/-/g, ' ').trim();

            const distMatch = clean.match(/((?:[\u4e00-\u9fa5]{2,3}[市縣])?[\u4e00-\u9fa5]{2,4}[區市鎮鄉])/);
            if (distMatch) return distMatch[1].trim();

            return clean || '未提供地址';
        }

        function isSubsidyHouse(fullText) {
            const subsidyTerms = SYNONYM_GROUPS[0];
            return subsidyTerms.some(kw => fullText.includes(kw));
        }

        function getExpandedSearchTerms(input) {
            if (!input) return [];
            let terms = [input];
            for (const group of SYNONYM_GROUPS) {
                if (group.some(kw => input.includes(kw) || kw.includes(input))) {
                    terms.push(...group);
                }
            }
            return Array.from(new Set(terms));
        }

        function filterAndRender() {
            const searchText = document.getElementById('searchInput').value.toLowerCase().trim();
            const sortVal = document.getElementById('sortSelect').value;
            const expandedTerms = getExpandedSearchTerms(searchText);

            let filtered = allHouses.filter(h => {
                const cleanAddr = cleanAddressDisplay(h.address);
                const fullText = `${h.title} ${cleanAddr} ${h.price} ${h.size} ${h.house_id} ${h.details_text || ''}`.toLowerCase();
                
                if (searchText) {
                    const matchesAnyTerm = expandedTerms.some(term => fullText.includes(term));
                    if (!matchesAnyTerm) return false;
                }

                if (currentFilter === 'off_market') return h.status === 'off_market';
                if (currentFilter === 'unrated') return (!h.user_rating || h.user_rating === 'none') && h.status !== 'off_market';
                if (currentFilter === 'like') return h.user_rating === 'like';
                if (currentFilter === 'neutral') return h.user_rating === 'neutral';
                if (currentFilter === 'dislike') return h.user_rating === 'dislike';
                if (currentFilter === 'subsidy') return isSubsidyHouse(fullText);
                if (currentFilter === 'taipower') return h.cost_info && h.cost_info.is_taipower;
                if (currentFilter === 'balcony') return SYNONYM_GROUPS[1].some(kw => fullText.includes(kw));
                if (currentFilter === 'washing') return SYNONYM_GROUPS[2].some(kw => fullText.includes(kw));
                if (currentFilter === 'below_market') return h.market && (h.market.level === 'cheap' || h.market.level === 'below');
                if (currentFilter === 'dropped') return !!h.price_drop;
                if (currentFilter !== 'all') return (h.address || '').includes(currentFilter);

                return true;
            });

            filtered.sort((a, b) => {
                const costA = a.cost_info ? a.cost_info.total_estimated_cost : 0;
                const costB = b.cost_info ? b.cost_info.total_estimated_cost : 0;
                const rentA = a.numeric_price || 0;
                const rentB = b.numeric_price || 0;
                const parseSqft = (str) => {
                    const m = (str || '').match(/(\\d+(?:\\.\\d+)?)/);
                    return m ? parseFloat(m[1]) : 0;
                };
                if (sortVal === 'cost_asc') return costA - costB;
                if (sortVal === 'rent_asc') return rentA - rentB;
                if (sortVal === 'size_desc') return parseSqft(b.size) - parseSqft(a.size);
                if (sortVal === 'time_desc') return new Date(b.created_at || 0) - new Date(a.created_at || 0);
                if (sortVal === 'market_asc') {
                    const ma = a.market ? a.market.diff_pct : 99999;
                    const mb = b.market ? b.market.diff_pct : 99999;
                    return ma - mb;
                }
                if (sortVal === 'drop_desc') {
                    const da = a.price_drop ? a.price_drop.total_drop : -1;
                    const dbv = b.price_drop ? b.price_drop.total_drop : -1;
                    return dbv - da;
                }
                return 0;
            });

            renderGrid(filtered);
        }

        function esc(s) {
            return String(s == null ? '' : s)
                .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        }

        function renderMarket(m) {
            if (!m) return '';
            const sign = m.diff_pct > 0 ? '+' : '';
            return `
                <div style="margin-top:10px;">
                    <span class="badge-market market-${esc(m.level)}">${esc(m.label)} ${sign}${m.diff_pct}%</span>
                    <div class="market-detail">
                        每坪 ${m.unit_cost.toLocaleString()} 元 ・ ${esc(m.scope)}中位數 ${m.median_unit_cost.toLocaleString()} 元
                    </div>
                </div>`;
        }

        function renderPriceDrop(d) {
            if (!d) return '';
            const times = d.change_count > 1 ? `，共調整 ${d.change_count} 次` : '';
            return `
                <div style="margin-top:8px;">
                    <span class="badge-drop">📉 已降 ${d.total_drop.toLocaleString()} 元 (${d.drop_pct}%)</span>
                    <div class="drop-detail">
                        <s>${d.original_price.toLocaleString()}</s> ➔ ${d.current_price.toLocaleString()} 元${times}
                        ${d.last_change_time ? '・最近 ' + esc(d.last_change_time.slice(0, 10)) : ''}
                    </div>
                </div>`;
        }

        function toggleDuplicates(houseId) {
            const el = document.getElementById('dups-' + houseId);
            if (el) el.style.display = (el.style.display === 'none' ? 'block' : 'none');
        }

        const RATING_ICON = { like: '❤️ 喜歡', neutral: '😐 普通', dislike: '💔 不喜歡' };

        function renderDuplicates(h) {
            const dups = h.duplicates || [];
            if (!dups.length) return '';

            const rows = dups.map(d => {
                const r = RATING_ICON[d.user_rating] || '';
                return `
                <div class="dup-item">
                    <a href="${esc(d.link)}" target="_blank" rel="noopener noreferrer">${esc(d.title)}</a>
                    <span>${esc(d.price)} ・ ${esc(d.size)} ・ ${esc(d.address)} ・ ID ${esc(d.house_id)}
                        ${r ? `<b class="dup-rating">${r}</b>` : ''}</span>
                </div>`;
            }).join('');

            // 收合的那幾筆若有評分，一定要在收合狀態下就看得到，否則等於評分憑空消失
            const rated = dups.filter(d => d.user_rating && d.user_rating !== 'none');
            const hint = rated.length
                ? `，其中 ${rated.map(d => RATING_ICON[d.user_rating]).join('、')}`
                : '';
            return `
                <div class="dup-block">
                    <button class="dup-toggle" onclick="toggleDuplicates('${esc(h.house_id)}')">
                        🔁 另有 ${dups.length} 筆相同刊登${hint}（點擊展開）
                    </button>
                    <div class="dup-list" id="dups-${esc(h.house_id)}" style="display:none;">${rows}</div>
                </div>`;
        }

        function renderGrid(houses) {
            const container = document.getElementById('listingsContainer');
            if (houses.length === 0) {
                container.innerHTML = '<div class="no-data">未找到符合篩選條件的房屋物件。</div>';
                return;
            }

            container.innerHTML = houses.map(h => {
                const cost = h.cost_info || {};
                const warnings = h.couples_warnings || [];
                const features = h.couples_features || [];
                const cleanAddr = cleanAddressDisplay(h.address);
                const fullText = `${h.title} ${cleanAddr} ${h.details_text || ''}`;
                const hasSubsidy = isSubsidyHouse(fullText);
                const rating = h.user_rating || 'none';
                const status = h.status || 'active';

                return `
                    <div class="house-card rated-${rating} status-${status}">
                        <div class="card-header">
                            ${status === 'off_market' ? '<div class="badge-offmarket">🏚️ 已下架/已出租</div>' : ''}
                            ${hasSubsidy && status !== 'off_market' ? '<div class="badge-taipower" style="background:rgba(16, 185, 129, 0.15); color:#34d399; border-color:rgba(16, 185, 129, 0.3);">📜 可申請租屋補助</div>' : ''}
                            ${cost.is_taipower && status !== 'off_market' ? '<div class="badge-taipower">✨ 台電省錢神房 (台電計費)</div>' : (!hasSubsidy && status !== 'off_market' ? '<div class="badge-normal">🏠 特選優質物件</div>' : '')}
                            ${rating === 'none' && status !== 'off_market' ? '<div class="badge-unrated">❓ 尚未評分</div>' : ''}
                            <a href="${esc(h.link)}" target="_blank" rel="noopener noreferrer" class="house-title">${esc(h.title)}</a>
                        </div>
                        <div class="meta-pills">
                            <span class="meta-tag">📍 ${esc(cleanAddr)}</span>
                            <span class="meta-tag">📐 ${esc(h.size || '未提供坪數')}</span>
                            <span class="meta-tag">🆔 ${esc(h.house_id)}</span>
                        </div>
                        <div class="cost-block">
                            <div class="cost-title">預估真實月總成本 (${cost.electricity_kwh || 400}度用電)</div>
                            <div class="cost-amount">${cost.total_estimated_cost_str || h.price}</div>
                            ${renderMarket(h.market)}
                            ${renderPriceDrop(h.price_drop)}
                            <div class="cost-details">
                                <div>💰 租金: ${esc(h.price)}</div>
                                <div>🏢 管理費/額外費: ${esc(cost.management_desc || '0元')}</div>
                                <div>⚡ 電費: ${esc(cost.electricity_desc || '內含')}</div>
                                <div>💧 水雜費: ${esc(cost.water_desc || '0元')}</div>
                            </div>
                        </div>
                        ${renderDuplicates(h)}
                        <div class="tags-section">
                            ${warnings.map(w => `<div class="tag-warning">${esc(w)}</div>`).join('')}
                            <div>
                                ${features.map(f => `<span class="tag-feature">${esc(f)}</span>`).join('')}
                            </div>
                        </div>
                        
                        <div class="rating-toolbar">
                            <div class="rating-btn-group">
                                <button class="rating-btn ${rating === 'like' ? 'active-like' : ''}" onclick="setHouseRating('${h.house_id}', 'like')">❤️ 喜歡</button>
                                <button class="rating-btn ${rating === 'neutral' ? 'active-neutral' : ''}" onclick="setHouseRating('${h.house_id}', 'neutral')">😐 普通</button>
                                <button class="rating-btn ${rating === 'dislike' ? 'active-dislike' : ''}" onclick="setHouseRating('${h.house_id}', 'dislike')">💔 不喜歡</button>
                            </div>
                            <a href="${h.link}" target="_blank" class="btn-link">🔗 591 頁面 ➔</a>
                        </div>
                    </div>
                `;
            }).join('');
        }

        fetchHouses();
        // 資料每 10 分鐘才由巡邏更新一次，30 秒輪詢一次純粹是浪費頻寬
        // （免費額度只有 5GB，開著一個分頁一天就能吃掉 2GB）。
        // 搭配伺服器端的 ETag，沒變動時回 304，這裡的間隔再放寬也不影響即時性。
        setInterval(fetchHouses, 120000);
    </script>
</body>
</html>"""


def render_dashboard_html(page_title: str = DEFAULT_PAGE_TITLE) -> str:
    """產生完整儀表板 HTML。回傳純字串，不經過 Jinja 樣板引擎。"""
    return HTML_TEMPLATE.replace("__PAGE_TITLE__", page_title)
