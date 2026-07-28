import os
import sys
import json
import logging
from playwright.sync_api import sync_playwright
from config import COOKIES_FILE, USER_AGENTS
import random

logging.basicConfig(level=logging.INFO)

def login_and_save_cookies(login_url="https://rent.591.com.tw"):
    print("=== 進入手動登入模式 ===")
    print("請在開啟的瀏覽器視窗中手動完成登入動作，完成後回到此終端機按 Enter 鍵繼續...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(user_agent=random.choice(USER_AGENTS))
        page = context.new_page()
        page.goto(login_url)

        input("\n登入完成後請按 [Enter] 鍵以匯出 Cookies...")

        cookies = context.cookies()
        with open(COOKIES_FILE, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        
        print(f"✅ 成功匯出 {len(cookies)} 個 Cookies 至 {COOKIES_FILE}")
        browser.close()

if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "https://rent.591.com.tw"
    login_and_save_cookies(url)
