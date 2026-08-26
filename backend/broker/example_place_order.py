"""
backend/broker/example_place_order.py

群益證券台指期貨下單範例（純命令列，不含 GUI）。

執行前請確認：
  1. 已完成 backend/broker/capital_futures.py 檔頭所述的環境設置
     （SKCOM.dll 註冊、VC++ 2010 Redistributable、comtypes 安裝）。
  2. 已於專案根目錄 .env 設定 CAPITAL_USER_ID / CAPITAL_PASSWORD
     （可參考 .env.example）。
  3. 強烈建議先以 CAPITAL_ENVIRONMENT=2（測試環境 + 測試帳號）驗證下單流程，
     確認無誤後才切換到正式環境（0）進行實盤交易。

執行方式：
    python -m backend.broker.example_place_order --product MXFR1 --side buy --qty 1
"""

from __future__ import annotations

import argparse
import logging
import os

from dotenv import load_dotenv

from backend.broker.capital_futures import (
    BuySell,
    CapitalFuturesBroker,
    Environment,
    NewClose,
    TradeType,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="群益證券台指期貨下單範例")
    parser.add_argument("--product", default="MXFR1", help="期貨商品代碼（含月份），例如小台近月 MXFR1")
    parser.add_argument("--side", choices=["buy", "sell"], required=True, help="買進或賣出")
    parser.add_argument("--qty", type=int, default=1, help="下單口數")
    parser.add_argument("--price", default="0", help="委託價格；搭配 --fok 可視為市價")
    parser.add_argument("--fok", action="store_true", help="使用 FOK（全部成交否則取消），預設為 ROD")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    load_dotenv()
    environment = int(os.getenv("CAPITAL_ENVIRONMENT", Environment.PRODUCTION))

    broker = CapitalFuturesBroker(environment=environment)
    broker.login()  # 從 .env 讀取 CAPITAL_USER_ID / CAPITAL_PASSWORD

    accounts = broker.initialize_order()
    if not accounts:
        raise RuntimeError("未取得任何交易帳號，請確認登入帳號具備期貨交易權限。")
    account = accounts[0]
    logging.info("使用交易帳號: %s", account)

    result = broker.send_future_order(
        account=account,
        product_code=args.product,
        side=BuySell.BUY if args.side == "buy" else BuySell.SELL,
        qty=args.qty,
        price=args.price,
        trade_type=TradeType.FOK if args.fok else TradeType.ROD,
        new_close=NewClose.AUTO,
    )
    logging.info("下單結果: %s", result)

    # 等待委託回報（OnNewData）送達
    broker.pump_events(3.0)


if __name__ == "__main__":
    main()
