import json
import re
import os
import sys
import time
import random
import logging
import threading
from flask import Flask, jsonify, render_template_string

from db import HousingDB
from cost_calculator import parse_rental_costs
from scraper import RentalScraper
from notifier import DiscordNotifier
from config import DISCORD_WEBHOOK_URL, DB_PATH, CHECK_INTERVAL_MINUTES

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("OurHomeCloudApp")

app = Flask(__name__)
db = HousingDB(DB_PATH)
notifier = DiscordNotifier(DISCORD_WEBHOOK_URL)
scraper = RentalScraper()

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OurHome 雙人同住租屋品質與成本儀表板 (雲端 24H 版)</title>
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

        .refresh-btn {
            background: linear-gradient(135deg, #0284c7, #2563eb);
            color: white; border: none; padding: 10px 18px; border-radius: 8px;
            font-weight: 600; font-size: 14px; cursor: pointer; transition: all 0.2s ease;
            box-shadow: 0 4px 12px rgba(37, 99, 235, 0.3); display: flex; align-items: center; gap: 8px;
        }

        .refresh-btn:hover { opacity: 0.9; transform: translateY(-1px); }

        .stats-grid {
            display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
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
        }

        .house-card:hover {
            transform: translateY(-3px); border-color: rgba(56, 189, 248, 0.4);
            box-shadow: 0 10px 24px rgba(0, 0, 0, 0.3);
        }

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
            display: block; width: 100%; text-align: center; background: rgba(255, 255, 255, 0.08);
            border: 1px solid var(--card-border); color: var(--text-main); padding: 10px;
            border-radius: 8px; font-size: 13px; font-weight: 600; text-decoration: none; margin-top: 14px; transition: all 0.2s;
        }

        .btn-link:hover { background: #0284c7; border-color: #38bdf8; color: white; }
        .no-data { text-align: center; padding: 60px; color: var(--text-sub); font-size: 16px; grid-column: 1 / -1; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="brand">
                <h1>👩‍❤️‍👨 OurHome 雙人同住租屋網頁儀表板</h1>
                <p>雲端 24H 自動巡邏台北市 5 區特選房源與台電省錢神房</p>
            </div>
            <button class="refresh-btn" onclick="fetchHouses()">🔄 立即刷新</button>
        </header>

        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-label">適合雙人房源總數</div>
                <div class="stat-value highlight-blue" id="stat-total">0 筆</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">✨ 台電計費省錢神房</div>
                <div class="stat-value highlight-yellow" id="stat-taipower">0 筆</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">平均雙人預估月總成本</div>
                <div class="stat-value highlight-green" id="stat-avg-cost">$0</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">最低雙人預估月總成本</div>
                <div class="stat-value" id="stat-min-cost">$0</div>
            </div>
        </div>

        <div class="controls-card">
            <input type="text" id="searchInput" class="search-box" placeholder="🔍 輸入關鍵字或路名搜尋 (如：忠孝東路、北安路、大安、獨洗、台電...)" oninput="filterAndRender()">

            <div class="filters-row">
                <div class="pill-group" id="filterPills">
                    <div class="pill active" data-filter="all" onclick="setFilter('all', this)">全部房源</div>
                    <div class="pill" data-filter="taipower" onclick="setFilter('taipower', this)">✨ 台電神房</div>
                    <div class="pill" data-filter="balcony" onclick="setFilter('balcony', this)">🧺 有獨立陽台</div>
                    <div class="pill" data-filter="washing" onclick="setFilter('washing', this)">🧺 有獨立洗衣機</div>
                    <div class="pill" data-filter="大安區" onclick="setFilter('大安區', this)">📍 大安區</div>
                    <div class="pill" data-filter="中山區" onclick="setFilter('中山區', this)">📍 中山區</div>
                    <div class="pill" data-filter="信義區" onclick="setFilter('信義區', this)">📍 信義區</div>
                    <div class="pill" data-filter="松山區" onclick="setFilter('松山區', this)">📍 松山區</div>
                    <div class="pill" data-filter="南港區" onclick="setFilter('南港區', this)">📍 南港區</div>
                </div>

                <select id="sortSelect" class="sort-select" onchange="filterAndRender()">
                    <option value="cost_asc">排序：預估雙人總成本 (低 ➔ 高)</option>
                    <option value="rent_asc">排序：刊登租金 (低 ➔ 高)</option>
                    <option value="size_desc">排序：坪數 (大 ➔ 小)</option>
                    <option value="time_desc">排序：最新上架/更新時間</option>
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

        async function fetchHouses() {
            try {
                const res = await fetch('/api/houses');
                allHouses = await res.json();
                updateStats();
                filterAndRender();
            } catch (err) {
                console.error("載入房屋列表失敗:", err);
            }
        }

        function updateStats() {
            const total = allHouses.length;
            const taipowerCount = allHouses.filter(h => h.cost_info && h.cost_info.is_taipower).length;
            let totalCostSum = 0, minCost = 999999;

            allHouses.forEach(h => {
                const cost = h.cost_info ? h.cost_info.total_estimated_cost : 0;
                if (cost > 0) {
                    totalCostSum += cost;
                    if (cost < minCost) minCost = cost;
                }
            });

            const avgCost = total > 0 ? Math.round(totalCostSum / total) : 0;
            document.getElementById('stat-total').innerText = `${total} 筆`;
            document.getElementById('stat-taipower').innerText = `${taipowerCount} 筆`;
            document.getElementById('stat-avg-cost').innerText = `$${avgCost.toLocaleString()} /月`;
            document.getElementById('stat-min-cost').innerText = minCost < 999999 ? `$${minCost.toLocaleString()} /月` : '$0';
        }

        function setFilter(filterType, element) {
            currentFilter = filterType;
            document.querySelectorAll('.pill').forEach(p => p.classList.remove('active'));
            element.classList.add('active');
            filterAndRender();
        }

        function filterAndRender() {
            const searchText = document.getElementById('searchInput').value.toLowerCase().trim();
            const sortVal = document.getElementById('sortSelect').value;

            let filtered = allHouses.filter(h => {
                const fullStr = `${h.title} ${h.address} ${h.price} ${h.size} ${h.house_id}`.toLowerCase();
                if (searchText && !fullStr.includes(searchText)) return false;

                if (currentFilter === 'taipower') return h.cost_info && h.cost_info.is_taipower;
                if (currentFilter === 'balcony') return h.couples_features && h.couples_features.some(f => f.includes('陽台'));
                if (currentFilter === 'washing') return h.couples_features && h.couples_features.some(f => f.includes('洗衣機'));
                if (['大安區', '中山區', '信義區', '松山區', '南港區'].includes(currentFilter)) return fullStr.includes(currentFilter.toLowerCase());

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
                return 0;
            });

            renderGrid(filtered);
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

                return `
                    <div class="house-card">
                        <div class="card-header">
                            ${cost.is_taipower ? '<div class="badge-taipower">✨ 雙人省錢神房 (台電計費)</div>' : '<div class="badge-normal">👩‍❤️‍👨 特選雙人同住物件</div>'}
                            <a href="${h.link}" target="_blank" class="house-title">${h.title}</a>
                        </div>
                        <div class="meta-pills">
                            <span class="meta-tag">📍 ${h.address || '未提供地址'}</span>
                            <span class="meta-tag">📐 ${h.size || '未提供坪數'}</span>
                            <span class="meta-tag">🆔 ${h.house_id}</span>
                        </div>
                        <div class="cost-block">
                            <div class="cost-title">預估雙人真實月總成本 (400度用電)</div>
                            <div class="cost-amount">${cost.total_estimated_cost_str || h.price}</div>
                            <div class="cost-details">
                                <div>💰 租金: ${h.price}</div>
                                <div>🏢 管理費: ${cost.management_desc || '0元'}</div>
                                <div>⚡ 電費: ${cost.electricity_desc || '內含'}</div>
                                <div>💧 水雜費: ${cost.water_desc || '0元'}</div>
                            </div>
                        </div>
                        <div class="tags-section">
                            ${warnings.map(w => `<div class="tag-warning">${w}</div>`).join('')}
                            <div>${features.map(f => `<span class="tag-feature">${f}</span>`).join('')}</div>
                        </div>
                        <a href="${h.link}" target="_blank" class="btn-link">🔗 一鍵直達 591 房屋頁面 ➔</a>
                    </div>
                `;
            }).join('');
        }

        fetchHouses();
        setInterval(fetchHouses, 30000);
    </script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route("/api/houses")
def api_houses():
    houses = db.get_all_houses()
    for h in houses:
        full_text = f"{h.get('title', '')} {h.get('address', '')}"
        h["cost_info"] = parse_rental_costs(full_text, h.get("price", "0"))
        h["couples_warnings"] = scraper.detect_couples_warnings(full_text)
        h["couples_features"] = scraper.detect_couples_features(full_text)
    return jsonify(houses)

def background_crawler_loop():
    logger.info("啟動 24H 雲端自動巡邏背景線程...")
    while True:
        try:
            logger.info("=== 開始執行 24H 雲端巡邏與推播任務 ===")
            fetched = scraper.run()
            if fetched:
                for house in fetched:
                    full_text = f"{house.get('title', '')} {house.get('address', '')}"
                    house["cost_info"] = parse_rental_costs(full_text, house.get("price", "0"))
                batch_res = db.process_houses_batch(fetched)
                new_houses = batch_res.get("new_houses", [])
                price_drops = batch_res.get("price_drop_houses", [])
                
                if new_houses:
                    notifier.batch_notify(new_houses, is_price_drop=False)
                if price_drops:
                    notifier.batch_notify(price_drops, is_price_drop=True)

        except Exception as e:
            logger.error(f"雲端巡邏任務異常: {e}")

        sleep_seconds = CHECK_INTERVAL_MINUTES * 60 + random.randint(-30, 30)
        logger.info(f"巡邏結束，等待 {sleep_seconds} 秒後進行下一次巡邏...")
        time.sleep(sleep_seconds)

# 啟動背景巡邏線程 (Daemon)
crawler_thread = threading.Thread(target=background_crawler_loop, daemon=True)
crawler_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
