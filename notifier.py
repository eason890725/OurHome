import time
import random
import requests
import logging
from typing import Dict, Any, List
from config import ENV_NAME

logger = logging.getLogger(__name__)

class DiscordNotifier:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def send_house_card(self, house: Dict[str, Any], is_price_drop: bool = False) -> bool:
        """
        發送雙人同住模式房屋資訊至 Discord Webhook (自動標註 💻 本地 PC 或 ☁️ Render 雲端)
        """
        cost_info = house.get("cost_info", {})
        is_taipower = cost_info.get("is_taipower", False)
        
        if not self.webhook_url:
            card_type = "🚨 降價警報" if is_price_drop else "🏠 新上架"
            logger.info(f"[未設定 Discord Webhook] 模擬發送通知 ({card_type}) [{ENV_NAME}]:\n"
                        f"物件：{house.get('title')}\n"
                        f"租金：{house.get('price')} (原價: {house.get('old_price', 'N/A')})\n"
                        f"預估真實雙人月成本 (400度)：{cost_info.get('total_estimated_cost_str', '未估算')}\n"
                        f"台電神房：{'是' if is_taipower else '否'}\n"
                        f"警示：{house.get('couples_warnings')}\n"
                        f"配備：{house.get('couples_features')}\n"
                        f"連結：{house.get('link')}\n")
            return True

        # 卡片主題與標題
        if is_price_drop:
            color = 15158332  # 鮮紅色
            title_prefix = f"🚨 【雙人降價警報！直降 {house.get('drop_amount', '')}】"
        elif is_taipower:
            color = 5763719   # 金黃色 (台電神房)
            title_prefix = "✨ 【雙人省錢神房（台電計費）】"
        else:
            color = 3066993   # 翡翠綠/藍色
            title_prefix = "👩‍❤️‍👨 【雙人精選新上架】"

        title_text = f"{title_prefix} {house.get('title', '無標題')}"

        # 費用細項 (400度用電)
        cost_breakdown_str = (
            f"💰 **刊登租金**：{house.get('price', '未提供')}\n"
            f"🏢 **管理費**：{cost_info.get('management_desc', '未標示')}\n"
            f"⚡ **預估電費 (雙人400度)**：{cost_info.get('electricity_desc', '未標示')}\n"
            f"💧 **水費/雜費**：{cost_info.get('water_desc', '未標示')}"
        )

        total_cost_display = f"💵 **` {cost_info.get('total_estimated_cost_str', '未計算')} `**"

        fields = [
            {
                "name": "🏷️ 刊登價格狀態",
                "value": f"~~{house.get('old_price')}~~ ➡️ **{house.get('price')}**" if is_price_drop else house.get("price", "未提供"),
                "inline": True
            },
            {
                "name": "📐 坪數 (舒適雙人大空間)",
                "value": house.get("size", "未提供"),
                "inline": True
            },
            {
                "name": "📍 地址",
                "value": house.get("address", "未提供"),
                "inline": False
            },
            {
                "name": "📊 預估雙人真實月總成本 (租金+管理費+雙人電費400度+水雜費)",
                "value": total_cost_display,
                "inline": False
            },
            {
                "name": "📝 費用細項明細 (含400度電費)",
                "value": cost_breakdown_str,
                "inline": False
            }
        ]

        # 雙人警示標籤
        warnings = house.get("couples_warnings", [])
        if warnings:
            fields.append({
                "name": "⚠️ 雙人同住注意事項與警示",
                "value": "\n".join(warnings),
                "inline": False
            })

        # 雙人優質機能標籤
        features = house.get("couples_features", [])
        if features:
            fields.append({
                "name": "🌟 雙人舒適生活配備標籤",
                "value": " • ".join(features),
                "inline": False
            })

        fields.append({
            "name": "🆔 房屋 ID",
            "value": house.get("house_id", "未知"),
            "inline": True
        })

        fields.append({
            "name": "🖥️ 巡邏發送來源",
            "value": f"**{ENV_NAME}**",
            "inline": True
        })

        embed = {
            "title": title_text[:250],
            "url": house.get("link", ""),
            "color": color,
            "fields": fields,
            "footer": {
                "text": f"OurHome 雙人同住租屋品質與成本監控系統 • 來源: {ENV_NAME}"
            }
        }

        payload = {
            "username": f"雙人好房速報 Bot [{ENV_NAME}]",
            "avatar_url": "https://cdn-icons-png.flaticon.com/512/619/619032.png",
            "embeds": [embed]
        }

        try:
            for attempt in range(3):
                res = requests.post(self.webhook_url, json=payload, timeout=10)
                if res.status_code in (200, 204):
                    logger.info(f"成功發送 Discord 通知 [{ENV_NAME}] ({'降價警報' if is_price_drop else '新物件'}): {house.get('title')}")
                    return True
                elif res.status_code == 429:
                    try:
                        retry_after = res.json().get("retry_after", 1.0)
                    except Exception:
                        retry_after = 1.5
                    logger.warning(f"Discord 觸發頻率限制 (HTTP 429)，避讓等待 {retry_after:.2f} 秒後重試...")
                    time.sleep(retry_after + 0.5)
                else:
                    logger.error(f"發送 Discord 通知失敗 HTTP {res.status_code}: {res.text}")
                    return False
            return False
        except Exception as e:
            logger.error(f"發送 Discord 通知異常: {e}")
            return False

    def notify_new_house(self, house: Dict[str, Any]) -> bool:
        return self.send_house_card(house, is_price_drop=False)

    def notify_price_drop(self, house: Dict[str, Any], old_price: str = "", drop_amount: str = "") -> bool:
        house_copy = dict(house)
        if old_price:
            house_copy["old_price"] = old_price
        if drop_amount:
            house_copy["drop_amount"] = drop_amount
        return self.send_house_card(house_copy, is_price_drop=True)

    def batch_notify(self, houses: List[Dict[str, Any]], is_price_drop: bool = False):
        for house in houses:
            self.send_house_card(house, is_price_drop=is_price_drop)
            time.sleep(random.uniform(0.8, 1.2))

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    notifier = DiscordNotifier(webhook_url="")
    notifier.send_house_card({
        "house_id": "777666",
        "title": "測試雙人獨立陽台套房",
        "price": "23,000 元/月",
        "address": "台北市大安區羅斯福路",
        "size": "10.5 坪",
        "cost_info": {
            "rent": 23000,
            "management_fee": 0,
            "management_desc": "內含 (0 元)",
            "is_taipower": True,
            "electricity_fee": 1000,
            "electricity_desc": "台電帳單計費 (400度約 1,000 元/月)",
            "water_fee": 0,
            "water_desc": "台水/包水 (0~100 元)",
            "total_estimated_cost": 24000,
            "total_estimated_cost_str": "24,000 元/月"
        },
        "couples_warnings": ["⚠️ 儲熱式熱水器 (連續洗澡熱水可能不足)"],
        "couples_features": ["🧺 獨立陽台", "🧺 獨立洗衣機"],
        "link": "https://rent.591.com.tw/777666"
    })
