import re
import logging
from typing import Dict, Any
from config import DEFAULT_ELECTRICITY_KWH, MODE_LABEL

logger = logging.getLogger(__name__)

def parse_numeric(val_str: str) -> int:
    """提取純數字"""
    if not val_str:
        return 0
    clean = re.sub(r'[^\d]', '', str(val_str))
    return int(clean) if clean else 0

def parse_rental_costs(text: str, price_str: str, electricity_kwh: int = DEFAULT_ELECTRICITY_KWH) -> Dict[str, Any]:
    """
    費用計算器（動態支援單人 200度 / 雙人 400度用電）：
    從房屋標題、內文及591結構化文字中萃取費用細項（包含『另有額外費用X元/月』）並估算預估月總成本。
    """
    full_text = text or ""
    rent = parse_numeric(price_str)

    # 1. 解析管理費 / 另有額外費用
    management_fee = 0
    management_desc = "內含 / 無管理費"
    
    extra_match = re.search(r'(?:另有)?額外費用[：:\s]*([\d,]+)\s*元', full_text)
    if extra_match:
        fee_val = parse_numeric(extra_match.group(1))
        # 防呆：避免額外費用被誤抓為租金金額 (限制在 10,000 元以內)
        if 0 < fee_val <= 10000:
            management_fee = fee_val
            management_desc = f"額外費用/管理費 {management_fee:,} 元/月"

    if management_fee == 0:
        if re.search(r'(含管|包管|內含管理費|含管理費|免管理費|無管理費|不收管理費)', full_text):
            management_fee = 0
            management_desc = "內含 (0 元)"
        else:
            mgmt_match = re.search(r'管理費[：:\s]*([\d,]+)\s*元', full_text)
            if not mgmt_match:
                mgmt_match = re.search(r'([\d,]+)\s*元\s*/?\s*月?\s*管理費', full_text)
            
            if mgmt_match:
                fee_val = parse_numeric(mgmt_match.group(1))
                if 0 < fee_val <= 10000:
                    management_fee = fee_val
                    management_desc = f"{management_fee:,} 元/月"
                else:
                    management_desc = "內含 / 未標示"
            else:
                management_desc = "內含 / 未標示"

    # 2. 解析電費 (單人預設 200度，雙人預設 400度)
    is_taipower = False
    electricity_fee = electricity_kwh * 5  # 預設非台電 5 元/度
    electricity_desc = f"約 {electricity_fee:,} 元 (預估一度5元/{electricity_kwh}度)"

    if re.search(r'(台電|依台電|台電計費|公電分攤|台灣電力)', full_text):
        is_taipower = True
        electricity_fee = int(electricity_kwh * 2.5)  # 台電平均約 2.5 元/度
        electricity_desc = f"台電帳單計費 ({electricity_kwh}度約 {electricity_fee:,} 元/月)"
    else:
        rate_match = re.search(r'(?:一度|1度|電費|電費一度)[：:\s]*([\d\.]+)\s*元', full_text)
        if not rate_match:
            rate_match = re.search(r'([\d\.]+)\s*元\s*/?\s*(?:度|kWh)', full_text)

        if rate_match:
            try:
                rate = float(rate_match.group(1))
                # 防呆：限制電費單價在合理範圍 (一度 2 ~ 10 元)
                if 2.0 <= rate <= 10.0:
                    electricity_fee = int(rate * electricity_kwh)
                    electricity_desc = f"一度 {rate} 元 ({electricity_kwh}度約 {electricity_fee:,} 元/月)"
            except ValueError:
                pass

    # 3. 解析水費
    water_fee = 100
    water_desc = "約 100 元/月"

    if re.search(r'(台水|包水|含水|水費內含|免水費|不收水費)', full_text):
        water_fee = 0
        water_desc = "台水/包水 (0~100 元)"
    else:
        water_match = re.search(r'水費[：:\s]*([\d,]+)\s*元', full_text)
        if water_match:
            w_val = parse_numeric(water_match.group(1))
            if 0 < w_val <= 2000:
                water_fee = w_val
                water_desc = f"{water_fee:,} 元/月"

    # 4. 解析其他雜費 (如垃圾清潔費，防呆限制 <= 2000 元，避免誤抓租金或押金)
    other_fees = 0
    other_desc = "無特別雜費"

    trash_match = re.search(r'(?:垃圾|清潔費|代收費)[：:\s]*([\d,]+)\s*元', full_text)
    if trash_match and "免" not in trash_match.group(0) and "含" not in trash_match.group(0):
        fee_val = parse_numeric(trash_match.group(1))
        if 0 < fee_val <= 2000:
            other_fees = fee_val
            other_desc = f"垃圾/清潔費 {other_fees:,} 元/月"

    # 5. 公式：預估月總成本
    total_estimated_monthly_cost = rent + management_fee + electricity_fee + water_fee + other_fees

    return {
        "rent": rent,
        "management_fee": management_fee,
        "management_desc": management_desc,
        "is_taipower": is_taipower,
        "electricity_fee": electricity_fee,
        "electricity_desc": electricity_desc,
        "water_fee": water_fee,
        "water_desc": water_desc,
        "other_fees": other_fees,
        "other_desc": other_desc,
        "total_estimated_cost": total_estimated_monthly_cost,
        "total_estimated_cost_str": f"{total_estimated_monthly_cost:,} 元/月",
        "mode_label": MODE_LABEL,
        "electricity_kwh": electricity_kwh
    }

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
