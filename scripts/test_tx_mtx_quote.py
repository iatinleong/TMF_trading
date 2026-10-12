"""測試大台/小台的報價連續碼（TX00/MTX00）能不能正常訂閱到即時報價。

只做報價訂閱，完全不碰下單，不會動用到真錢。跟 scripts/test_live_quote.py
是同樣的獨立連線方式，用自己的一組 CapitalFuturesBroker 連線，不會影響
VM 上正在跑的共用連線/已武裝的策略。

使用方式（要先在 .env 設定 CAPITAL_USER_ID / CAPITAL_PASSWORD）：
    python -m scripts.test_tx_mtx_quote
"""
import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

base_dir = Path(__file__).resolve().parent.parent
load_dotenv(base_dir / ".env")

try:
    from backend.broker.capital_futures import CapitalFuturesBroker
except ImportError:
    import sys
    sys.path.append(str(base_dir))
    from backend.broker.capital_futures import CapitalFuturesBroker

PRODUCTS_TO_TEST = [
    ("TX00", "大台（TX）報價連續碼"),
    ("MTX00", "小台（MTX）報價連續碼"),
]


def _test_one(broker: CapitalFuturesBroker, product: str, label: str, *, wait_seconds: float = 15.0) -> bool:
    print(f"\n=== 測試 {label}：{product} ===")
    try:
        broker.subscribe_quote(product)
    except Exception as exc:  # noqa: BLE001
        print(f"[失敗] 訂閱 {product} 時就直接拋例外: {exc}")
        return False

    deadline = time.monotonic() + wait_seconds
    got_price = False
    while time.monotonic() < deadline:
        broker.pump_events(0.5)
        state = broker.get_live_state()
        quote = state.get("quote") or {}
        if quote.get("last_price") is not None:
            print(f"[成功] {product} 收到報價：last_price={quote['last_price']}, "
                  f"updated_at={quote.get('updated_at')}")
            got_price = True
            break

    if not got_price:
        print(f"[失敗] {product} 等了 {wait_seconds} 秒都沒收到任何報價"
              f"（可能代號無效、未開盤，或商品檔還沒下載完成）")
    return got_price


def main() -> None:
    user_id = os.getenv("CAPITAL_USER_ID")
    password = os.getenv("CAPITAL_PASSWORD")
    env_str = os.getenv("CAPITAL_ENVIRONMENT", "2")  # 預設 2=測試環境
    try:
        environment = int(env_str)
    except ValueError:
        environment = 2

    if not user_id or not password:
        print("【錯誤】請先在 .env 設定 CAPITAL_USER_ID 與 CAPITAL_PASSWORD 再執行")
        return

    print(f"正在以帳號 {user_id} 連線至環境 {environment}（0=正式, 2=測試）...")
    broker = CapitalFuturesBroker(environment=environment)
    broker.login(user_id, password)
    print("登入完成，開始逐一測試報價代碼（純訂閱，不下單）...")

    results = {}
    for product, label in PRODUCTS_TO_TEST:
        results[product] = _test_one(broker, product, label)

    print("\n=== 總結 ===")
    for product, label in PRODUCTS_TO_TEST:
        status = "可用 ✓" if results[product] else "無法確認 ✗"
        print(f"{label}（{product}）：{status}")


if __name__ == "__main__":
    main()
