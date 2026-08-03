# -*- coding: utf-8 -*-
"""容器記憶體監測。

Render 的 OOM 事件只會說「used over 512MB」，不會說是誰用的。
這個模組定期把「本程序」與「容器內所有程序」的 RSS 寫進 log，
用來分辨到底是 Web 程序、爬蟲子程序，還是多個程序累積造成的。

只依賴 /proc（Linux），在 Windows 上會安靜地不做事。
"""
import os
import threading
import time
import logging

logger = logging.getLogger(__name__)

PROC_AVAILABLE = os.path.isdir("/proc")


def _rss_kb(pid: str = "self") -> int:
    try:
        with open(f"/proc/{pid}/status", "r") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except Exception:
        pass
    return 0


def _cmd(pid: str) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            parts = f.read().split(b"\0")
        return " ".join(p.decode("utf-8", "replace") for p in parts if p)[:60]
    except Exception:
        return "?"


def snapshot() -> str:
    """回傳一行容器內各程序的記憶體概況。"""
    if not PROC_AVAILABLE:
        return ""
    total = 0
    rows = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        kb = _rss_kb(pid)
        if kb <= 0:
            continue
        total += kb
        rows.append((kb, pid, _cmd(pid)))
    rows.sort(reverse=True)
    detail = " | ".join(f"{kb / 1024:.0f}MB pid={pid} {cmd.split('/')[-1][:28]}"
                        for kb, pid, cmd in rows[:5])
    pct = total / 1024 / 512 * 100
    return f"🧠 容器總計 {total / 1024:.0f}MB / 512MB ({pct:.0f}%)｜{detail}"


def log_now(tag: str = ""):
    line = snapshot()
    if line:
        logger.info(f"{line}{('  ← ' + tag) if tag else ''}")


def start_monitor(interval_seconds: int = 60):
    """背景定期記錄記憶體。重複呼叫只會啟動一次。"""
    if not PROC_AVAILABLE or getattr(start_monitor, "_started", False):
        return
    start_monitor._started = True

    def loop():
        while True:
            try:
                log_now()
            except Exception:
                pass
            time.sleep(interval_seconds)

    threading.Thread(target=loop, daemon=True).start()
    logger.info(f"🧠 記憶體監測已啟動（每 {interval_seconds} 秒記錄一次）")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(snapshot() or "此平台無 /proc，無法量測")
