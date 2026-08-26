# -*- coding: utf-8 -*-
"""
最小化報價連線診斷腳本（單一進程、單一執行緒）。

流程：login -> RequestAgreement(印同意書狀態) -> EnterMonitorLONG ->
持續 pump 事件並每 5 秒印一次 SKQuoteLib_IsConnected()，
所有 OnConnection 事件都會由 broker 的 logger 印出。

判讀：
  - IsConnected 一直停在 2 且無任何 [OnConnection] ⇒ 環境/權限問題（找營業員）
  - 收到 kind=3001 再到 kind=3003 ⇒ 報價連線正常，改查訂閱端
"""
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

base_dir = Path(__file__).resolve().parent.parent
load_dotenv(base_dir / ".env")
sys.path.insert(0, str(base_dir))

from backend.broker.capital_futures import CapitalFuturesBroker  # noqa: E402

WATCH_SECONDS = 180


def main() -> None:
    user_id = os.getenv("CAPITAL_USER_ID")
    password = os.getenv("CAPITAL_PASSWORD")
    env = int(os.getenv("CAPITAL_ENVIRONMENT", "0"))
    if not user_id or not password:
        print("請先在 .env 設定 CAPITAL_USER_ID / CAPITAL_PASSWORD")
        return

    print(f"=== 環境={env} (0=正式)  觀察 {WATCH_SECONDS} 秒 ===")
    broker = CapitalFuturesBroker(environment=env)
    broker.login(user_id, password)
    print(f"--- 登入成功，同意書狀態（共 {len(broker.live.agreements)} 筆）---")
    for row in broker.live.agreements:
        print(f"  [同意書] {row}")

    print("--- 呼叫 SKReplyLib_ConnectByID（連線回報主機，新版 API 必要前置步驟）---")
    n_code = broker.reply.SKReplyLib_ConnectByID(user_id)
    print(f"  ConnectByID code={n_code} ({broker.center.SKCenterLib_GetReturnCodeMessage(n_code)})")
    broker.pump_events(2.0)

    print("--- 呼叫 EnterMonitorLONG ---")
    broker.enter_monitor()

    start = time.time()
    last_status = None
    while time.time() - start < WATCH_SECONDS:
        broker.pump_events(1.0)
        status = broker.quote.SKQuoteLib_IsConnected()
        elapsed = int(time.time() - start)
        if status != last_status:
            print(f"  [{elapsed:3d}s] IsConnected={status} (0=斷線 1=連線中 2=下載中)")
            last_status = status
        elif elapsed % 15 == 0:
            print(f"  [{elapsed:3d}s] IsConnected={status} stocks_ready={broker.is_quote_ready()}")
        if broker.is_quote_ready():
            print(f"  [{elapsed:3d}s] >>> 收到 3003，商品檔下載完成！報價連線正常。")
            break
        time.sleep(0.2)

    print("=== 結束 ===")
    print(f"stocks_ready={broker.is_quote_ready()}")
    print("quote_conn 事件紀錄：")
    for m in broker.live.messages:
        if m["category"] == "quote_conn":
            print(f"  {m['time']} {m['text']}")
    if not broker.is_quote_ready():
        print("\n結論：OnConnection 3003 未出現 —— 請將上方輸出提供給營業員，")
        print("重點詢問：1) API 下單聲明書/行情同意書是否生效  2) 元件版本是否需更新")


if __name__ == "__main__":
    main()
