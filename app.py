import json
import re
import os
import sys
import time
import random
import logging
import threading
import subprocess
import requests
from flask import Flask, jsonify, render_template_string, request

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

_HOUSES_CACHE = []
_CACHE_LAST_UPDATE = 0

def get_formatted_houses_cached():
    global _HOUSES_CACHE, _CACHE_LAST_UPDATE
    now = time.time()
    if _HOUSES_CACHE and (now - _CACHE_LAST_UPDATE < 5):
        return _HOUSES_CACHE
    
    houses = db.get_all_houses()
    for h in houses:
        full_text = f"{h.get('title', '')} {h.get('address', '')} {h.get('details_text', '')}"
        h["cost_info"] = parse_rental_costs(full_text, h.get("price", "0"))
        h["couples_warnings"] = scraper.detect_couples_warnings(full_text)
        h["couples_features"] = scraper.detect_couples_features(full_text)
    
    _HOUSES_CACHE = houses
    _CACHE_LAST_UPDATE = now
    return _HOUSES_CACHE

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OurHome 租屋品質與成本儀表板 (雲端 24H 版)</title>
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
            display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
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
                <div class="stat-label">❤️ 喜愛精選物件</div>
                <div class="stat-value highlight-red" id="stat-like">0 筆</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">✨ 台電計費神房</div>
                <div class="stat-value highlight-yellow" id="stat-taipower">0 筆</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">平均預估月總成本</div>
                <div class="stat-value highlight-green" id="stat-avg-cost">$0</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">最低預估月總成本</div>
                <div class="stat-value" id="stat-min-cost">$0</div>
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
                syncLocalRatingsToServer();

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

            const countRating = (r) => allHouses.filter(h => h.user_rating === r).length;

            const fixedPillsHtml = `
                <div class="pill ${currentFilter === 'all' ? 'active' : ''}" data-filter="all" onclick="setFilter('all', this)">全部房源</div>
                <div class="pill pill-like ${currentFilter === 'like' ? 'active' : ''}" data-filter="like" onclick="setFilter('like', this)">❤️ 喜歡的房源 (${countRating('like')})</div>
                <div class="pill pill-neutral ${currentFilter === 'neutral' ? 'active' : ''}" data-filter="neutral" onclick="setFilter('neutral', this)">😐 普通紀錄 (${countRating('neutral')})</div>
                <div class="pill pill-dislike ${currentFilter === 'dislike' ? 'active' : ''}" data-filter="dislike" onclick="setFilter('dislike', this)">💔 不喜歡/已淘汰 (${countRating('dislike')})</div>
                <div class="pill ${currentFilter === 'subsidy' ? 'active' : ''}" data-filter="subsidy" onclick="setFilter('subsidy', this)">📜 可租補</div>
                <div class="pill ${currentFilter === 'taipower' ? 'active' : ''}" data-filter="taipower" onclick="setFilter('taipower', this)">✨ 台電神房</div>
                <div class="pill ${currentFilter === 'balcony' ? 'active' : ''}" data-filter="balcony" onclick="setFilter('balcony', this)">🧺 有獨立陽台</div>
                <div class="pill ${currentFilter === 'washing' ? 'active' : ''}" data-filter="washing" onclick="setFilter('washing', this)">🧺 獨立洗衣機</div>
            `;

            const districtPillsHtml = Array.from(presentDistricts).sort().map(d => `
                <div class="pill ${currentFilter === d ? 'active' : ''}" data-filter="${d}" onclick="setFilter('${d}', this)">📍 ${d}</div>
            `).join('');

            pillGroup.innerHTML = fixedPillsHtml + districtPillsHtml;
        }

        function updateStats() {
            const total = allHouses.length;
            const likeCount = allHouses.filter(h => h.user_rating === 'like').length;
            const taipowerCount = allHouses.filter(h => h.cost_info && h.cost_info.is_taipower).length;
            let totalCostSum = 0, minCost = 999999;

            if (allHouses.length > 0 && allHouses[0].cost_info && allHouses[0].cost_info.mode_label) {
                document.getElementById('pageHeading').innerText = `🏠 OurHome ${allHouses[0].cost_info.mode_label}租屋儀表板`;
            }

            allHouses.forEach(h => {
                const cost = h.cost_info ? h.cost_info.total_estimated_cost : 0;
                if (cost > 0) {
                    totalCostSum += cost;
                    if (cost < minCost) minCost = cost;
                }
            });

            const avgCost = total > 0 ? Math.round(totalCostSum / total) : 0;
            document.getElementById('stat-total').innerText = `${total} 筆`;
            document.getElementById('stat-like').innerText = `${likeCount} 筆`;
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

        function cleanAddressDisplay(rawAddr) {
            if (!rawAddr) return '未提供地址';
            let clean = rawAddr.replace(/^(依現場|社區名稱|所屬社區|高樓層|電梯大樓|大廈|無|未知)+/g, '').trim();
            clean = clean.replace(/(整層住家出租|整層住家|獨立套房出租|獨立套房|分租套房|雅房|住家出租|住家|出租)+$/g, '').trim();

            const roadMatch = clean.match(/((?:[\u4e00-\u9fa5]{2,3}[市縣])?[\u4e00-\u9fa5]{2,4}[區市鎮鄉][\s\-–—─]*[\u4e00-\u9fa5\dA-Za-z]+(?:路|街|段|巷|弄|號|大道)?)/);
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

                if (currentFilter === 'like') return h.user_rating === 'like';
                if (currentFilter === 'neutral') return h.user_rating === 'neutral';
                if (currentFilter === 'dislike') return h.user_rating === 'dislike';
                if (currentFilter === 'subsidy') return isSubsidyHouse(fullText);
                if (currentFilter === 'taipower') return h.cost_info && h.cost_info.is_taipower;
                if (currentFilter === 'balcony') return SYNONYM_GROUPS[1].some(kw => fullText.includes(kw));
                if (currentFilter === 'washing') return SYNONYM_GROUPS[2].some(kw => fullText.includes(kw));
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
                const cleanAddr = cleanAddressDisplay(h.address);
                const fullText = `${h.title} ${cleanAddr} ${h.details_text || ''}`;
                const hasSubsidy = isSubsidyHouse(fullText);
                const rating = h.user_rating || 'none';

                return `
                    <div class="house-card rated-${rating}">
                        <div class="card-header">
                            ${hasSubsidy ? '<div class="badge-taipower" style="background:rgba(16, 185, 129, 0.15); color:#34d399; border-color:rgba(16, 185, 129, 0.3);">📜 可申請租屋補助</div>' : ''}
                            ${cost.is_taipower ? '<div class="badge-taipower">✨ 台電省錢神房 (台電計費)</div>' : (!hasSubsidy ? '<div class="badge-normal">🏠 特選優質物件</div>' : '')}
                            <a href="${h.link}" target="_blank" class="house-title">${h.title}</a>
                        </div>
                        <div class="meta-pills">
                            <span class="meta-tag">📍 ${cleanAddr}</span>
                            <span class="meta-tag">📐 ${h.size || '未提供坪數'}</span>
                            <span class="meta-tag">🆔 ${h.house_id}</span>
                        </div>
                        <div class="cost-block">
                            <div class="cost-title">預估真實月總成本 (${cost.electricity_kwh || 400}度用電)</div>
                            <div class="cost-amount">${cost.total_estimated_cost_str || h.price}</div>
                            <div class="cost-details">
                                <div>💰 租金: ${h.price}</div>
                                <div>🏢 管理費/額外費: ${cost.management_desc || '0元'}</div>
                                <div>⚡ 電費: ${cost.electricity_desc || '內含'}</div>
                                <div>💧 水雜費: ${cost.water_desc || '0元'}</div>
                            </div>
                        </div>
                        <div class="tags-section">
                            ${warnings.map(w => `<div class="tag-warning">${w}</div>`).join('')}
                            <div>
                                ${features.map(f => `<span class="tag-feature">${f}</span>`).join('')}
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
    houses = get_formatted_houses_cached()
    return jsonify(houses)

@app.route("/api/rating", methods=["POST"])
def api_rating():
    try:
        data = request.get_json(force=True)
        house_id = str(data.get("house_id", ""))
        rating = str(data.get("rating", "none"))
        success = db.update_house_rating(house_id, rating)
        return jsonify({"success": success, "house_id": house_id, "rating": rating})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/sync_ratings", methods=["POST"])
def api_sync_ratings():
    try:
        data = request.get_json(force=True)
        ratings = data.get("ratings", {})
        synced_count = 0
        for hid, r in ratings.items():
            if db.update_house_rating(str(hid), str(r)):
                synced_count += 1
        return jsonify({"success": True, "synced_count": synced_count})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

def keep_render_alive():
    """發送 HTTP Ping 防止 Render 免費伺服器因 15 分鐘無人存取而休眠 (24H 防休眠保活)"""
    try:
        render_url = os.environ.get("RENDER_EXTERNAL_URL") or "https://ourhome-aiwq.onrender.com"
        resp = requests.get(f"{render_url}/", timeout=10)
        logger.info(f"⚡ 防休眠 Ping 成功 ({render_url}) [Status: {resp.status_code}]")
    except Exception as e:
        logger.debug(f"防休眠 Ping 提示: {e}")

def background_crawler_loop():
    logger.info("啟動 24H 雲端自動巡邏背景獨立行程機制...")
    time.sleep(10)
    while True:
        try:
            logger.info("=== 喚醒獨立子程序爬蟲 (完全不佔用或鎖定 Web GIL) ===")
            proc = subprocess.Popen([sys.executable, "run_crawler_standalone.py"])
            proc.wait()
        except Exception as e:
            logger.error(f"調用獨立爬蟲失敗: {e}")

        sleep_seconds = CHECK_INTERVAL_MINUTES * 60 + random.randint(-30, 30)
        logger.info(f"巡邏結束，等待 {sleep_seconds} 秒後進行下一次巡邏...")
        
        elapsed = 0
        while elapsed < sleep_seconds:
            time.sleep(300)
            elapsed += 300
            keep_render_alive()

crawler_thread = threading.Thread(target=background_crawler_loop, daemon=True)
crawler_thread.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
