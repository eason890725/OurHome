# -*- coding: utf-8 -*-
"""從房源文字中萃取雙人同住相關的警示與配備標籤。

**這個模組刻意不 import playwright。**
Web 程序（app.py / dashboard.py）只需要這兩個純 regex 函式，
但它們原本掛在 RentalScraper 上，導致 Flask 程序為了呼叫它們而整包載入 Playwright，
在 Render 512MB 的容器裡是無謂的記憶體開銷。爬蟲另有獨立子程序負責。
"""
import re
from typing import List


def detect_couples_warnings(text: str) -> List[str]:
    """雙人同住的注意事項。"""
    warnings = []
    if re.search(r'(第二人|多1人|兩人入住加價|加收費用|加價|每多一人)', text or ""):
        warnings.append("⚠️ 第二人入住需額外加價/補貼")
    if re.search(r'(儲熱式|儲熱型|電熱水器)', text or ""):
        warnings.append("⚠️ 儲熱式熱水器 (連續洗澡熱水可能不足)")
    return warnings


def detect_couples_features(text: str) -> List[str]:
    """雙人同住的加分配備。"""
    features = []
    if re.search(r'(獨立陽台|獨陽|有陽台|陽台)', text or ""):
        features.append("🧺 獨立陽台")
    if re.search(r'(獨立洗衣機|獨洗|獨洗獨曬|個人洗衣機)', text or ""):
        features.append("🧺 獨立洗衣機")
    if re.search(r'(雙人床|雙人雙層|雙人大床)', text or ""):
        features.append("🛏️ 雙人床配置")
    return features
