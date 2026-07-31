# -*- coding: utf-8 -*-
"""大眾運輸通勤時間計算。

用 Google Maps Distance Matrix API 算出每個房源到指定目的地的**大眾運輸**通勤時間。

設計重點：
- **需要 API 金鑰才會啟用**。沒設定 `GOOGLE_MAPS_API_KEY` 就整個功能靜默關閉，
  儀表板不顯示通勤欄位，其餘功能完全不受影響。
- **結果會快取進資料庫**。同一個地址只查一次，因為地址對應的通勤時間不會變。
  這讓 API 用量從「每次巡邏 × 房源數」降到「新增房源數」。
- **批次查詢**。Distance Matrix 單次最多 25 個起點 × 25 個終點，
  因此 160 筆房源 × 2 個目的地只需要 7 次請求。
"""
import os
import re
import json
import logging
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

DISTANCE_MATRIX_URL = "https://maps.googleapis.com/maps/api/distancematrix/json"

# 單次請求的起點數上限（Google 規定 origins × destinations <= 100，且各自 <= 25）
MAX_ORIGINS_PER_REQUEST = 25


def get_destinations() -> List[str]:
    """從環境變數讀取通勤目的地，以逗號分隔。"""
    raw = os.getenv("COMMUTE_DESTINATIONS", "捷運南京復興站,捷運軟體園區站")
    return [d.strip() for d in raw.split(",") if d.strip()]


def is_enabled() -> bool:
    return bool(os.getenv("GOOGLE_MAPS_API_KEY", "").strip())


def normalize_origin(address: str) -> str:
    """把房源地址整理成適合送進 Distance Matrix 的字串。

    資料裡的地址多半沒有縣市（例如「中山區民生東路一段」），補上「台北市」可大幅提高定位準確度。
    新北市的行政區則補「新北市」。
    """
    if not address:
        return ""
    addr = address.strip()
    if "市" in addr[:3] or "縣" in addr[:3]:
        return addr

    NEW_TAIPEI = {"板橋區", "新莊區", "中和區", "永和區", "三重區", "新店區", "土城區",
                  "蘆洲區", "汐止區", "樹林區", "淡水區", "三峽區", "林口區", "鶯歌區",
                  "五股區", "泰山區", "八里區"}
    for d in NEW_TAIPEI:
        if addr.startswith(d):
            return "新北市" + addr
    return "台北市" + addr


class CommuteCalculator:
    def __init__(self, api_key: Optional[str] = None, destinations: Optional[List[str]] = None):
        self.api_key = (api_key if api_key is not None else os.getenv("GOOGLE_MAPS_API_KEY", "")).strip()
        self.destinations = destinations if destinations is not None else get_destinations()

    def get_durations(self, origins: List[str]) -> Dict[str, Dict[str, int]]:
        """查詢多個起點到所有目的地的大眾運輸時間（分鐘）。

        回傳 {原始地址: {目的地: 分鐘數}}。查不到的起點不會出現在結果中，
        查不到的個別目的地則不會出現在該起點的字典裡。
        """
        if not self.api_key or not origins or not self.destinations:
            return {}

        results: Dict[str, Dict[str, int]] = {}
        usable = [o for o in origins if normalize_origin(o)]

        for i in range(0, len(usable), MAX_ORIGINS_PER_REQUEST):
            batch = usable[i:i + MAX_ORIGINS_PER_REQUEST]
            batch_result = self._query_batch(batch)
            results.update(batch_result)

        return results

    def _query_batch(self, origins: List[str]) -> Dict[str, Dict[str, int]]:
        params = {
            "origins": "|".join(normalize_origin(o) for o in origins),
            "destinations": "|".join(self.destinations),
            "mode": "transit",
            "language": "zh-TW",
            "region": "tw",
            "key": self.api_key,
        }
        try:
            resp = requests.get(DISTANCE_MATRIX_URL, params=params, timeout=15)
            if resp.status_code != 200:
                logger.error(f"❌ 通勤查詢 HTTP {resp.status_code}: {resp.text[:200]}")
                return {}
            data = resp.json()
        except Exception as e:
            logger.error(f"❌ 通勤查詢異常: {e}")
            return {}

        status = data.get("status")
        if status != "OK":
            # REQUEST_DENIED 通常是金鑰沒開通 Distance Matrix 或未啟用帳單
            logger.error(f"❌ 通勤 API 回應狀態 {status}: {data.get('error_message', '')}")
            return {}

        out: Dict[str, Dict[str, int]] = {}
        for origin_addr, row in zip(origins, data.get("rows", [])):
            per_dest: Dict[str, int] = {}
            for dest, element in zip(self.destinations, row.get("elements", [])):
                if element.get("status") != "OK":
                    continue
                seconds = (element.get("duration") or {}).get("value")
                if isinstance(seconds, (int, float)) and seconds > 0:
                    per_dest[dest] = int(round(seconds / 60))
            if per_dest:
                out[origin_addr] = per_dest
        return out


def backfill(db, limit: int = 200) -> int:
    """幫資料庫裡還沒有通勤時間的地址補查並寫入快取，回傳新增筆數。

    在每輪巡邏結束時呼叫。因為有快取，穩定之後每輪只會查到新增的幾筆。
    `limit` 是單輪上限，避免第一次執行時一口氣打太多請求。
    """
    if not is_enabled():
        return 0
    pending = db.get_addresses_without_commute()
    if not pending:
        return 0

    if len(pending) > limit:
        logger.info(f"🚇 待查通勤地址 {len(pending)} 筆，本輪先處理 {limit} 筆，其餘下輪繼續")
        pending = pending[:limit]

    logger.info(f"🚇 開始查詢 {len(pending)} 個地址的大眾運輸通勤時間...")
    results = CommuteCalculator().get_durations(pending)
    if not results:
        logger.warning("⚠️ 通勤查詢沒有取得任何結果")
        return 0
    db.save_commute_cache(results)
    return len(results)


def summarize(commute: Optional[Dict[str, int]]) -> Optional[Dict[str, object]]:
    """把 {目的地: 分鐘} 整理成前端好用的形式，附上最長通勤時間作為排序依據。"""
    if not commute:
        return None
    items = [{"dest": short_dest_name(d), "minutes": m} for d, m in commute.items()]
    items.sort(key=lambda x: x["minutes"])
    return {
        "items": items,
        "max_minutes": max(i["minutes"] for i in items),
        "min_minutes": min(i["minutes"] for i in items),
    }


def short_dest_name(dest: str) -> str:
    """去掉「捷運」前綴與「站」後綴，例如「捷運市政府站」→「市政府」，讓標籤不要太長。"""
    short = re.sub(r'站$', '', re.sub(r'^(台北捷運|捷運)', '', dest.strip()))
    return short or dest


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("通勤功能啟用:", is_enabled())
    print("目的地:", get_destinations())
    if is_enabled():
        calc = CommuteCalculator()
        print(calc.get_durations(["大安區羅斯福路三段", "內湖區內湖路一段"]))
