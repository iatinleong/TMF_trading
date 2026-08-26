"""对照实验：验证截图中的两个假设。

假设 A：历史 K 线下载（同步）会卡住报价通道 → 应先等 3003 再下 K 线
假设 B：商品代码错误（TMFR1 vs TMF00）导致一直下载中

用法: python scripts/test_quote_hypothesis.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from backend.broker.capital_futures import CapitalFuturesBroker, Environment


def _pump(broker: CapitalFuturesBroker, seconds: float) -> None:
    broker.pump_events(seconds)


def _wait_ready(broker: CapitalFuturesBroker, timeout: float = 120.0) -> bool:
    deadline = time.time() + timeout
    last_status = -1
    while time.time() < deadline:
        status = broker._quote_connection_status()
        if status != last_status:
            print(f"  IsConnected={status} stocks_ready={broker._stocks_ready}")
            last_status = status
        if broker._stocks_ready:
            return True
        _pump(broker, 0.5)
    return broker._stocks_ready


def _try_ticks(broker: CapitalFuturesBroker, product: str) -> int:
    code = broker.request_ticks(product)
    _pump(broker, 2.0)
    return code


def _try_kline(broker: CapitalFuturesBroker, product: str) -> int:
    code = broker.request_kline_history(product, days=5)
    _pump(broker, 2.0)
    return code


def _run_case(
    name: str,
    product: str,
    *,
    load_kline_early: bool,
    wait_before_subscribe: float,
) -> dict:
    print(f"\n{'='*60}")
    print(f"CASE: {name}")
    print(f"  product={product} kline_early={load_kline_early} wait={wait_before_subscribe}s")
    print("=" * 60)

    broker = CapitalFuturesBroker(environment=int(os.getenv("CAPITAL_ENVIRONMENT", Environment.PRODUCTION)))
    try:
        broker.login()
        broker.initialize_order(wait_seconds=5.0)
        broker.enter_monitor()
        _pump(broker, wait_before_subscribe)

        ready = _wait_ready(broker, timeout=90.0)
        print(f"  -> 3003 ready={ready} quote_status={broker._quote_connection_status()}")

        if load_kline_early and not ready:
            print("  -> 提早请求 K 线（模拟错误流程）...")
            k_code = _try_kline(broker, product)
            print(f"  -> RequestKLine early code={k_code} msg={broker._msg(k_code)}")
            _pump(broker, 5.0)
            ready = _wait_ready(broker, timeout=60.0)
            print(f"  -> 提早 K 线后 ready={ready} status={broker._quote_connection_status()}")

        tick_code = _try_ticks(broker, product)
        print(f"  -> RequestTicks code={tick_code} msg={broker._msg(tick_code)}")
        print(f"  -> last_price={broker.live.quote.last_price}")

        if ready and not load_kline_early:
            k_code = _try_kline(broker, product)
            print(f"  -> RequestKLine after ready code={k_code} msg={broker._msg(k_code)}")

        return {
            "name": name,
            "product": product,
            "ready": ready,
            "quote_status": broker._quote_connection_status(),
            "tick_code": tick_code,
            "last_price": broker.live.quote.last_price,
        }
    finally:
        broker.live.connected = False


def main() -> None:
    if sys.platform != "win32":
        print("仅 Windows 可运行 SKCOM 实验")
        sys.exit(1)

    cases = [
        ("A_错误流程_TMFR1_先K线", "TMFR1", True, 3.0),
        ("B_正确流程_TMF00_等3003", "TMF00", False, 3.0),
        ("C_正确流程_TMFR1_等3003", "TMFR1", False, 3.0),
        ("D_正确流程_TX00_等3003", "TX00", False, 3.0),
    ]

    results = []
    for name, product, kline_early, wait_sec in cases:
        try:
            results.append(
                _run_case(
                    name,
                    product,
                    load_kline_early=kline_early,
                    wait_before_subscribe=wait_sec,
                )
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  CASE FAILED: {exc}")
            results.append({"name": name, "error": str(exc)})
        time.sleep(5)

    print(f"\n{'='*60}")
    print("SUMMARY")
    print("=" * 60)
    for r in results:
        print(r)


if __name__ == "__main__":
    main()