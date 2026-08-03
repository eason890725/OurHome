# 爬蟲已改為純 HTTP，不再需要 Playwright／Chromium，
# 因此改用輕量的官方 Python 映像檔（原本是 mcr.microsoft.com/playwright/python，約 1.5GB）。
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=5000
EXPOSE 5000

CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", "--timeout", "120"]
