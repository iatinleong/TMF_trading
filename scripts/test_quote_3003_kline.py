# -*- coding: utf-8 -*-
"""
測試：報價機連線 + OnConnection 3003 + 歷史 K 線（蠟燭）。

流程：
  login → subscribe_quote（EnterMonitorLONG → 等 3003 → RequestTicks）
       → try_load_kline_history → 印出結果

用法（專案根目錄）:
  python scripts/test_quote_3003_kline.py
  python scripts/test_quote_3003_kline.py --product TMF00 --timeout 120 --bars 200
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent.parent
load_dotenv(BASE / ".env")
sys.path.insert(0, str(BASE))

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

from backend.broker.capital_futures import CapitalFuturesBroker, Environment  # noqa: E402
from backend.kline_engine import get_store  # noqa: E402
from backend.trading_service import resolve_quote_product_code  # noqa: E402


def _pass_fail(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def main() -> int:
    parser = argparse.ArgumentParser(description="測試報價 3003 + 歷史 K 線")
    parser.add_argument(
        "--product",
        default=os.getenv("LIVE_PRODUCT_CODE", "TMF00"),
        help="商品代碼（可用 TMF00 / TMFR1 / TX00 等）",
    )
    parser.add_argument("--timeout", type=float, default=120.0, help="等待 3003 秒數")
    parser.add_argument("--bars", type=int, default=200, help="請求歷史 K 棒上限")
    parser.add_argument(
        "--quote-wait",
        type=float,
        default=30.0,
        help="訂閱後等待 last_price 的秒數（RequestStocks/Ticks）",
    )
    args = parser.parse_args()

    user_id = os.getenv("CAPITAL_USER_ID")
    password = os.getenv("CAPITAL_PASSWORD")
    env = int(os.getenv("CAPITAL_ENVIRONMENT", str(Environment.PRODUCTION)))
    quote_code = resolve_quote_product_code(args.product)

    print("=" * 60)
    print("報價連線 / 3003 / 歷史 K 線 測試")
    print("=" * 60)
    print(f"  user_id     = {user_id}")
    print(f"  environment = {env} (0=正式 2=測試)")
    print(f"  product in  = {args.product}")
    print(f"  quote_code  = {quote_code}")
    print(f"  timeout     = {args.timeout}s")
    print(f"  bar_limit   = {args.bars}")
    print()

    if not user_id or not password:
        print("[FAIL] 缺少 CAPITAL_USER_ID / CAPITAL_PASSWORD（.env）")
        return 1

    results: dict[str, bool] = {}
    t0 = time.time()

    try:
        broker = CapitalFuturesBroker(environment=env)
        print(f"[1] DLL = {broker.dll_path}")
        broker.login(user_id, password, request_agreement=True)
        results["login"] = True
        print(f"[1] 登入 {_pass_fail(True)}  ({time.time() - t0:.1f}s)")
        if broker.live.agreements:
            print(f"    同意書事件 {len(broker.live.agreements)} 筆")
            for row in broker.live.agreements[:8]:
                print(f"      - {row}")
        if not broker.live.kline_api_ok:
            print(f"    [!] kline_api_ok=False: {broker.live.kline_api_block_reason}")
    except Exception as exc:
        results["login"] = False
        print(f"[1] 登入 FAIL: {exc}")
        _print_summary(results, quote_code, 0, None)
        return 1

    # 2) 報價訂閱（含 EnterMonitorLONG + 等 3003）
    t1 = time.time()
    print(f"\n[2] subscribe_quote({quote_code!r}) ...")
    quote_ok = broker.subscribe_quote(quote_code, wait_timeout=args.timeout)
    stocks_ready = broker.is_quote_ready()
    is_conn = broker._quote_connection_status()
    results["quote_3003"] = bool(stocks_ready)
    results["subscribe_quote"] = bool(quote_ok)
    print(f"[2] stocks_ready(3003) = {stocks_ready}  → {_pass_fail(stocks_ready)}")
    print(f"    IsConnected        = {is_conn} (0=斷 1=連線中 2=下載中 等)")
    print(f"    subscribe_quote    = {quote_ok}  → {_pass_fail(quote_ok)}")
    print(f"    耗時 {time.time() - t1:.1f}s")

    # quote_conn 訊息摘要
    quote_msgs = [m for m in broker.live.messages if m.get("category") == "quote_conn"]
    if quote_msgs:
        print("    quote_conn 事件：")
        for m in quote_msgs[-12:]:
            print(f"      {m.get('time')} {m.get('text')}")

    # 即時價：等 RequestStocks / Ticks / 快照寫入 last_price
    print(f"\n[3] 等待即時報價 last_price（最多 {args.quote_wait:.0f}s）...")
    t_quote = time.time()
    has_price = broker.wait_for_quote_price(timeout=args.quote_wait)
    q = broker.live.quote
    results["live_quote"] = has_price
    results["live_tick"] = broker._tick_notify_count > 0
    print(
        f"[3] last_price={q.last_price} bid={q.bid} ask={q.ask} updated={q.updated_at}"
    )
    print(
        f"    OnNotifyQuoteLONG 次數={broker._quote_notify_count}  "
        f"OnNotifyTicksLONG 次數={broker._tick_notify_count}  "
        f"→ live_quote {_pass_fail(has_price)}  "
        f"live_tick {_pass_fail(results['live_tick'])}"
    )
    print(f"    耗時 {time.time() - t_quote:.1f}s")
    quote_msgs2 = [m for m in broker.live.messages if m.get("category") in ("quote", "quote_conn")]
    for m in quote_msgs2[-8:]:
        print(f"      {m.get('time')} [{m.get('category')}] {m.get('text')}")

    # 4) 歷史 K 線（在即時訂閱之後）
    bar_count = 0
    sample = None
    if not stocks_ready:
        print("\n[4] 略過歷史 K 線（未取得 3003）")
        results["history_kline"] = False
    else:
        t2 = time.time()
        print(f"\n[4] try_load_kline_history({quote_code!r}, bar_limit={args.bars}) ...")
        kline_ok = broker.try_load_kline_history(quote_code, bar_limit=args.bars)
        broker.pump_events(2.0)

        store = get_store(quote_code)
        klines = store.get_klines(limit=args.bars)
        bar_count = len(klines)
        sample = klines[-3:] if klines else []
        kline_msgs = [m for m in broker.live.messages if m.get("category") == "kline"]
        results["history_kline"] = kline_ok and bar_count > 0

        print(f"[4] try_load 回傳     = {kline_ok}")
        print(f"    kline_api_ok      = {broker.live.kline_api_ok}")
        if broker.live.kline_api_block_reason:
            print(f"    block_reason      = {broker.live.kline_api_block_reason}")
        print(f"    store bar_count   = {bar_count}  → {_pass_fail(bar_count > 0)}")
        print(f"    kline 事件筆數    = {len(kline_msgs)}")
        print(f"    耗時 {time.time() - t2:.1f}s")
        if sample:
            print("    最近 K 線樣本：")
            for row in sample:
                print(
                    f"      time={row.get('time')} o={row.get('open')} h={row.get('high')} "
                    f"l={row.get('low')} c={row.get('close')} v={row.get('volume')}"
                )
        elif kline_msgs:
            print("    kline 訊息（前幾筆）：")
            for m in kline_msgs[:5]:
                print(f"      {m.get('text')}")

    # 5) K 線後再觀察報價是否仍在更新
    print("\n[5] K 線後再 pump 5 秒 ...")
    broker.pump_events(5.0)
    q = broker.live.quote
    print(
        f"[5] last_price={q.last_price} bid={q.bid} ask={q.ask} "
        f"quote_events={broker._quote_notify_count} tick_events={broker._tick_notify_count}"
    )
    if q.last_price is not None:
        results["live_quote"] = True

    print()
    _print_summary(results, quote_code, bar_count, q.last_price)
    print(f"總耗時 {time.time() - t0:.1f}s")

    # 成功條件：3003 + 歷史 K +（建議）即時 last_price
    if results.get("quote_3003") and results.get("history_kline") and results.get("live_quote"):
        return 0
    if results.get("quote_3003") and results.get("history_kline"):
        return 2  # 連上且有 K，但即時價未到
    if results.get("quote_3003"):
        return 3
    return 1


def _print_summary(
    results: dict[str, bool],
    quote_code: str,
    bar_count: int,
    last_price: float | None,
) -> None:
    print("=" * 60)
    print("摘要")
    print("=" * 60)
    for key in (
        "login",
        "quote_3003",
        "subscribe_quote",
        "live_quote",
        "live_tick",
        "history_kline",
    ):
        if key not in results:
            continue
        print(f"  {_pass_fail(results[key]):4s}  {key}")
    print(f"  quote_code={quote_code}  bars={bar_count}  last_price={last_price}")
    if results.get("quote_3003") and not results.get("live_quote"):
        print()
        print("提示：3003 已到但 last_price 仍空。")
        print("  - 已訂 RequestStocks（OnNotifyQuoteLONG）+ RequestTicks")
        print("  - 確認商品碼（微台 TMyymm，如 TM2608）")
        print("  - 盤中再試；或對照官方 Quote.py GUI")
    if results.get("quote_3003") and not results.get("history_kline"):
        print()
        print("提示：已連上報價（3003）但歷史 K 線為 0。")
        print("  - 檢查行情/API 同意書（kline_api_block_reason）")
        print("  - 商品代碼是否為有效月碼（例 TM2608）")
        print("  - 查看 data/capital_logs 的 Quote.log")
    if not results.get("quote_3003"):
        print()
        print("提示：未收到 3003。常見原因：")
        print("  - 非 Windows / SKCOM 未註冊")
        print("  - 帳號無行情權限或同意書未生效")
        print("  - 防火牆/網路阻擋報價主機")
        print("  - 可再跑: python scripts/diag_quote_events.py")


if __name__ == "__main__":
    raise SystemExit(main())
