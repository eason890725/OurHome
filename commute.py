# -*- coding: utf-8 -*-
"""大眾運輸通勤時間估算（完全離線，不需要任何 API 金鑰）。

做法：
1. 從房源標題與內文中找出提到的捷運站名（最長匹配，並排除「中山區」這類行政區誤判）。
2. 用內建的台北捷運路網圖，以 Dijkstra 算出到目的地的最少搭乘時間。

因此這是**估算值**，不是實際時刻表時間。模型很簡單：
每站 `MINUTES_PER_STOP` 分鐘、每次轉乘加 `TRANSFER_PENALTY` 分鐘、
再加上從房子走到捷運站的 `WALK_MINUTES` 分鐘。
用途是「快速比較哪些房源通勤比較方便」，不適合拿來抓準點到分鐘。

⚠️ 路網資料是人工整理的，若有站名或順序錯誤，直接修改下方 `MRT_LINES` 即可，
   不需要動其他程式碼。目前**未收錄環狀線**（該線經過的行政區不在預設搜尋範圍內）。
"""
import os
import re
import heapq
import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 每搭一站的估計時間（分鐘）
MINUTES_PER_STOP = 2.0
# 每次轉乘的估計時間（含走月台與候車）
TRANSFER_PENALTY = 5.0
# 從房子走到最近捷運站的估計時間
WALK_MINUTES = 5.0

# ── 台北捷運路網（依實際行駛順序排列）──────────────────────────────
MRT_LINES: Dict[str, List[str]] = {
    "文湖線": [
        "動物園", "木柵", "萬芳社區", "萬芳醫院", "辛亥", "麟光", "六張犁", "科技大樓",
        "大安", "忠孝復興", "南京復興", "中山國中", "松山機場", "大直", "劍南路", "西湖",
        "港墘", "文德", "內湖", "大湖公園", "葫洲", "東湖", "南港軟體園區", "南港展覽館",
    ],
    "淡水信義線": [
        "淡水", "紅樹林", "竹圍", "關渡", "忠義", "復興崗", "北投", "奇岩", "唭哩岸",
        "石牌", "明德", "芝山", "士林", "劍潭", "圓山", "民權西路", "雙連", "中山",
        "台北車站", "台大醫院", "中正紀念堂", "東門", "大安森林公園", "大安",
        "信義安和", "台北101/世貿", "象山",
    ],
    "新北投支線": ["北投", "新北投"],
    "松山新店線": [
        "松山", "南京三民", "台北小巨蛋", "南京復興", "松江南京", "中山", "北門", "西門",
        "小南門", "中正紀念堂", "古亭", "台電大樓", "公館", "萬隆", "景美", "大坪林",
        "七張", "新店區公所", "新店",
    ],
    "小碧潭支線": ["七張", "小碧潭"],
    "中和新蘆線_蘆洲": [
        "南勢角", "景安", "永安市場", "頂溪", "古亭", "東門", "忠孝新生", "松江南京",
        "行天宮", "中山國小", "民權西路", "大橋頭", "三重國小", "三和國中", "徐匯中學",
        "三民高中", "蘆洲",
    ],
    "中和新蘆線_迴龍": [
        "大橋頭", "台北橋", "菜寮", "三重", "先嗇宮", "頭前庄", "新莊", "輔大", "丹鳳", "迴龍",
    ],
    "板南線": [
        "頂埔", "永寧", "土城", "海山", "亞東醫院", "府中", "板橋", "新埔", "江子翠",
        "龍山寺", "西門", "台北車站", "善導寺", "忠孝新生", "忠孝復興", "忠孝敦化",
        "國父紀念館", "市政府", "永春", "後山埤", "昆陽", "南港", "南港展覽館",
    ],
}

# 站名的常見別名／簡寫
STATION_ALIASES: Dict[str, str] = {
    "台北車站": "台北車站",
    "北車": "台北車站",
    "劍南": "劍南路",
    "小巨蛋": "台北小巨蛋",
    "101": "台北101/世貿",
    "台北101": "台北101/世貿",
    "世貿": "台北101/世貿",
    "軟體園區": "南港軟體園區",
    "南軟": "南港軟體園區",
    "紀念堂": "中正紀念堂",
    "國館": "國父紀念館",
}

# 這些站名同時是行政區名，出現「○○區」時不可判定為捷運站
DISTRICT_COLLISIONS = {"中山", "松山", "大安", "北投", "士林", "南港", "內湖", "信義安和"}


def _all_stations() -> Dict[str, List[str]]:
    """{站名: [所屬路線, ...]}"""
    out: Dict[str, List[str]] = {}
    for line, stations in MRT_LINES.items():
        for s in stations:
            out.setdefault(s, []).append(line)
    return out


STATIONS = _all_stations()
# 最長優先，避免「中山」搶走「中山國小」
_STATION_NAMES_BY_LEN = sorted(
    set(list(STATIONS.keys()) + list(STATION_ALIASES.keys())), key=len, reverse=True
)


def canonical(name: str) -> Optional[str]:
    """把別名轉成正式站名；不是站名則回傳 None。"""
    name = (name or "").strip()
    if name in STATIONS:
        return name
    return STATION_ALIASES.get(name)


# ── 路網圖與最短時間 ────────────────────────────────────────────────
def _build_graph() -> Dict[Tuple[str, str], List[Tuple[Tuple[str, str], float]]]:
    """節點是 (站名, 路線)。同線相鄰站相連；同站不同線之間為轉乘邊。"""
    graph: Dict[Tuple[str, str], List[Tuple[Tuple[str, str], float]]] = {}

    def add(a, b, w):
        graph.setdefault(a, []).append((b, w))

    for line, stations in MRT_LINES.items():
        for i in range(len(stations) - 1):
            u, v = (stations[i], line), (stations[i + 1], line)
            add(u, v, MINUTES_PER_STOP)
            add(v, u, MINUTES_PER_STOP)

    for station, lines in STATIONS.items():
        for l1 in lines:
            for l2 in lines:
                if l1 != l2:
                    add((station, l1), (station, l2), TRANSFER_PENALTY)
    return graph


GRAPH = _build_graph()


def ride_minutes(origin: str, destination: str) -> Optional[Dict[str, float]]:
    """算出兩站之間的估計搭乘時間。回傳 {minutes, stops, transfers}，不可達則 None。"""
    o, d = canonical(origin), canonical(destination)
    if not o or not d:
        return None
    if o == d:
        return {"minutes": 0.0, "stops": 0, "transfers": 0}

    start_nodes = [(o, line) for line in STATIONS[o]]
    dist: Dict[Tuple[str, str], float] = {n: 0.0 for n in start_nodes}
    meta: Dict[Tuple[str, str], Tuple[int, int]] = {n: (0, 0) for n in start_nodes}
    pq = [(0.0, n) for n in start_nodes]
    heapq.heapify(pq)

    best: Optional[Dict[str, float]] = None
    while pq:
        cost, node = heapq.heappop(pq)
        if cost > dist.get(node, float("inf")):
            continue
        if node[0] == d:
            stops, transfers = meta[node]
            best = {"minutes": cost, "stops": stops, "transfers": transfers}
            break
        for nxt, w in GRAPH.get(node, []):
            nc = cost + w
            if nc < dist.get(nxt, float("inf")):
                dist[nxt] = nc
                s, t = meta[node]
                meta[nxt] = (s + 1, t) if nxt[0] != node[0] else (s, t + 1)
                heapq.heappush(pq, (nc, nxt))
    return best


# ── 從文字中找出捷運站 ──────────────────────────────────────────────
_METRO_CUE = re.compile(r'捷運|站|線')


def find_station(text: str) -> Optional[str]:
    """從房源標題／內文推測最近的捷運站。找不到就回傳 None。

    評分規則：站名旁邊有「捷運」或「站」等字樣者優先，其次取較長的站名。
    「中山區」這類行政區寫法會被排除。
    """
    if not text:
        return None

    best: Optional[Tuple[int, int, int, str]] = None  # (分數, 名稱長度, -位置, 正式站名)
    for name in _STATION_NAMES_BY_LEN:
        start = 0
        while True:
            idx = text.find(name, start)
            if idx < 0:
                break
            start = idx + 1
            end = idx + len(name)

            after = text[end:end + 1]
            before = text[max(0, idx - 2):idx]

            # 「中山區」「松山區」等行政區寫法不算捷運站
            if after == "區" and name in DISTRICT_COLLISIONS:
                continue
            # 「南京東路」不是「南京三民」之類的誤判：站名後面接「路/街/巷/弄/段/號」多半是路名
            if after in "路街巷弄段號":
                continue

            score = 1
            if "捷運" in before or after in ("站", "捷"):
                score = 3
            elif _METRO_CUE.search(text[end:end + 3] or ""):
                score = 2

            # 優先序：捷運字樣 > 出現位置較前 > 站名較長。
            # 位置優先於長度，是因為標題會排在內文之前，
            # 而標題寫的站名遠比內文順帶提到的可靠。
            cand = (score, -idx, len(name), canonical(name) or name)
            if best is None or cand > best:
                best = cand

    return best[3] if best else None


# ── 對外介面 ────────────────────────────────────────────────────────
def get_destinations() -> List[str]:
    raw = os.getenv("COMMUTE_DESTINATIONS", "南京復興,南港軟體園區")
    return [d.strip() for d in raw.split(",") if d.strip()]


def is_enabled() -> bool:
    """離線估算，永遠可用。留這個函式是為了讓呼叫端語意清楚。"""
    return True


def short_dest_name(dest: str) -> str:
    """去掉「捷運」前綴與「站」後綴，例如「捷運市政府站」→「市政府」。"""
    return re.sub(r'站$', '', re.sub(r'^(台北捷運|捷運)', '', (dest or "").strip())) or dest


def estimate(text: str, destinations: Optional[List[str]] = None,
             fallback_text: str = "", known_station: Optional[str] = None,
             walk_distance_m: Optional[int] = None) -> Optional[Dict[str, object]]:
    """估算某個房源到各目的地的通勤時間。

    `known_station` 是爬蟲從 591 卡片上直接取得的最近捷運站（「距○○站 N 公尺」），
    有的話一律優先採用——那是 591 自己標的，比從標題猜可靠得多。
    沒有才退回：先看 `text`（標題），再看 `fallback_text`（內文）。
    分兩段是因為內文常順帶提到別的車站，混在一起比對容易蓋掉標題寫明的站。

    `walk_distance_m` 有值時，步行時間依實際距離估算（時速約 80 公尺/分鐘），
    取代預設的固定 5 分鐘。

    回傳 {station, items: [{dest, minutes, stops, transfers}], max_minutes, min_minutes}
    找不到捷運站或所有目的地都不可達時回傳 None。
    """
    dests = destinations if destinations is not None else get_destinations()
    station = (canonical(known_station) if known_station else None) \
        or find_station(text) \
        or (find_station(fallback_text) if fallback_text else None)
    if not station or not dests:
        return None

    walk = WALK_MINUTES
    if walk_distance_m and walk_distance_m > 0:
        walk = max(1.0, round(walk_distance_m / 80.0))

    items = []
    for d in dests:
        r = ride_minutes(station, d)
        if r is None:
            continue
        items.append({
            "dest": short_dest_name(d),
            "minutes": int(round(r["minutes"] + walk)),
            "stops": r["stops"],
            "transfers": r["transfers"],
        })
    if not items:
        return None

    items.sort(key=lambda x: x["minutes"])
    return {
        "station": station,
        "items": items,
        "max_minutes": max(i["minutes"] for i in items),
        "min_minutes": min(i["minutes"] for i in items),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("收錄路線:", ", ".join(MRT_LINES.keys()))
    print("收錄站數:", len(STATIONS))
    print("預設目的地:", get_destinations())
    for t in ["🧸劍南路捷運✅可租補/可寵", "中山國小站近捷運2房", "🔆公館站超近🔆"]:
        print(" ", t, "→", estimate(t))
