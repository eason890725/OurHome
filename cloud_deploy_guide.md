# 🌐 永久免費 24H 雲端部署指南 (Render.com 平台)

本指南引導您將 OurHome 雙人同住租屋系統上傳至 Render.com 免費雲端託管平台，取得**永久免費的 24H 線上網址**！

---

## ⚡ 步驟 1：建立免費 GitHub 儲存庫 (Repository)

1. 開啟 [GitHub.com](https://github.com) 並登入（若無帳號可免費註冊）。
2. 點擊右上角 `+` ➔ `New repository`。
3. 專案名稱輸入 `OurHome-Rentals`，設為 `Private` (私有) 或 `Public` (公開)，點擊 `Create repository`。
4. 將本機專案資料夾 (`c:\personl\OurHome`) 上傳或 Push 至該 GitHub 儲存庫。

---

## 🚀 步驟 2：在 Render.com 建立免費雲端 Web 服務

1. 前往 [Render.com 官網](https://render.com) 點擊 **Sign Up**（建議直接點「Continue with GitHub」快速登入，**無需信用卡**）。
2. 在 Render 儀表板點擊右上角 **`New +`** ➔ 選擇 **`Web Service`**。
3. 在專案列表選取剛建立的 `OurHome-Rentals` 儲存庫並點擊 **Connect**。
4. 設定服務資訊：
   - **Name**: `ourhome-rentals` (將成為您的專屬網址前綴)
   - **Language / Environment**: 選擇 **`Docker`**（系統會自動讀取我們寫好的 `Dockerfile`，內建瀏覽器環境）
   - **Region**: 選擇 `Singapore` (新加坡) 或 `Oregon` (離台灣近且速度快)
   - **Instance Type**: 選擇 **`Free` ($0/month)**
5. 在下方點擊 **`Advanced`** ➔ **`Add Environment Variable`** 加入環境變數：
   - `DISCORD_WEBHOOK_URL` ➔ `https://discord.com/api/webhooks/1531485838343405608/YtQ_3utuHEWXBx6joJndRzFAjeboKi78L98rzd4kkjG6_W2S0Dgt3FzO6xeL497Vknzk`
   - `TARGET_URL_1` ➔ `https://rent.591.com.tw/list?region=1&section=1,4,5,7,11&kind=2&shape=2&notice=all_sex&rentprice=10000_20000,20000_30000`
   - `TARGET_URL_2` ➔ `https://rent.591.com.tw/list?region=1&section=4,7,3,11,5&price=10000_20000,20000_30000&kind=1&shape=2&notice=all_sex&layout=1,2`
6. 點擊最下方 **`Create Web Service`** 按鈕！

---

## 🎉 步驟 3：享受永久 24H 免費線上記錄網址

- Render 會自動開始構建與安裝環境，約需 2~3 分鐘。
- 完成後，畫面左上角會顯示您的專屬永久 HTTPS 網址（例如：`https://ourhome-rentals.onrender.com`）。
- **完成！** 今後就算您把家用電腦關機，Render 雲端伺服器也會 **24 小時不間斷巡邏 591，發送 Discord 通知，並讓您與另一半隨時在手機上登入查看最新房源！**
