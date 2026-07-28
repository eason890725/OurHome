import re
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

def parse_numeric(val_str: str) -> int:
    """提取純數字"""
    if not val_str:
        return 0
    clean = re.sub(r'[^\d]', '', str(val_str))
    return int(clean) if clean else 0

def parse_rental_costs(text: str, price_str: str) -> Dict[str, Any]:
    """
    雙人同住模式：
    從房屋標題、內文及591結構化文字中萃取費用細項（包含『另有額外費用X元/月』）並估算預估月總成本。
    假設情境：雙人每月用電 400 度。
    """
    full_text = text or ""
    rent = parse_numeric(price_str)

    # 1. 解析管理費 / 另有額外費用 (Management / Extra Fees)
    management_fee = 0
    management_desc = "內含 / 無管理費"
    
    # 優先匹配 591 特有的『另有額外費用X元/月』標籤
    extra_match = re.search(r'(?:另有)?額外費用[：:\s]*([\d,]+)\s*元', full_text)
    if extra_match:
        management_fee = parse_numeric(extra_match.group(1))
        management_desc = f"額外費用/管理費 {management_fee:,} 元/月"
    elif re.search(r'(含管|包管|內含管理費|含管理費|免管理費|無管理費|不收管理費)', full_text):
        management_fee = 0
        management_desc = "內含 (0 元)"
    else:
        mgmt_match = re.search(r'管理費[：:\s]*([\d,]+)\s*元', full_text)
        if not mgmt_match:
            mgmt_match = re.search(r'([\d,]+)\s*元\s*/?\s*月?\s*管理費', full_text)
        
        if mgmt_match:
            management_fee = parse_numeric(mgmt_match.group(1))
            management_desc = f"{management_fee:,} 元/月"
        else:
            management_desc = "內含 / 未標示"

    # 2. 解析電費 (Electricity Rate) - 雙人同住假設每月 400 度
    is_taipower = False
    electricity_fee = 2000  # 預設非台電 5 元 * 400 度 = 2,000 元
    electricity_desc = "約 2,000 元 (預估一度5元/400度)"

    if re.search(r'(台電|依台電|台電計費|公電分攤|台灣電力)', full_text):
        is_taipower = True
        electricity_fee = 1000  # 台電 400 度夏季平均約 1,000 元/月
        electricity_desc = "台電帳單計費 (400度約 1,000 元/月)"
    else:
        rate_match = re.search(r'(?:一度|1度|電費|電費一度)[：:\s]*([\d\.]+)\s*元', full_text)
        if not rate_match:
            rate_match = re.search(r'([\d\.]+)\s*元\s*/?\s*(?:度|kWh)', full_text)

        if rate_match:
            try:
                rate = float(rate_match.group(1))
                electricity_fee = int(rate * 400)
                electricity_desc = f"一度 {rate} 元 (400度約 {electricity_fee:,} 元/月)"
            except ValueError:
                pass

    # 3. 解析水費 (Water Fee)
    water_fee = 100
    water_desc = "約 100 元/月"

    if re.search(r'(台水|包水|含水|水費內含|免水費|不收水費)', full_text):
        water_fee = 0
        water_desc = "台水/包水 (0~100 元)"
    else:
        water_match = re.search(r'水費[：:\s]*([\d,]+)\s*元', full_text)
        if water_match:
            water_fee = parse_numeric(water_match.group(1))
            water_desc = f"{water_fee:,} 元/月"

    # 4. 解析其他雜費
    other_fees = 0
    other_desc = "無特別雜費"

    trash_match = re.search(r'(?:垃圾|清潔費|代收費)[：:\s]*([\d,]+)\s*元', full_text)
    if trash_match and "免" not in trash_match.group(0) and "含" not in trash_match.group(0):
        other_fees = parse_numeric(trash_match.group(1))
        other_desc = f"垃圾/清潔費 {other_fees:,} 元/月"

    # 5. 公式：預估月總成本 = 租金 + 管理費/額外費用 + 預估電費 (400度) + 水費 + 其他雜費
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
        "total_estimated_cost_str": f"{total_estimated_monthly_cost:,} 元/月"
    }

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sample_text = "14,500 元/月 二個月 租金包含水費、網路、瓦斯費，另有額外費用800元/月 總支出試算"
    res = parse_rental_costs(sample_text, "14,500元/月")
    print("額外費用 800 元解析測試:", res['management_desc'], "總估算成本:", res['total_estimated_cost_str'])
