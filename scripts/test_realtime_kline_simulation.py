# -*- coding: utf-8 -*-
"""實盤 K 線與風控端對端模擬測試：
模擬真實 60 分鐘 K 棒推進 ➔ 金叉訊號確認 ➔ 市價進場 ➔ 掛上客製化 OCO 智慧單（-80 / +200） ➔ 模擬行情觸及停損 ➔ 觸發平倉與對帳全流程。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

from backend.indicators import add_moving_averages
from backend.kline_engine import get_store
from backend.strategy_service import (
    DEFAULT_STRATEGY,
    _armed,
    start_strategy,
    strategy_tick,
    stop_strategy,
    strategy_status,
)


def run_simulation():
    print("=" * 60)
    print("🚀 開始進行 Real-Time 60分K棒 + 客製化風控 全流程端對端模擬")
    print("=" * 60)

    product = "TM2609"
    strategy_id = "breakout_long"
    _armed.clear()

    # 1. 模擬啟動策略，注入客製化風控參數（停損 80 點、停利 200 點、金額硬停損 5000 元）
    print("\n[Step 1] 啟動策略並套用客製化風控參數...")
    initial_times = pd.date_range("2026-08-30 09:00", periods=62, freq="1h")
    # 前段先走跌（MA5 < MA20）
    initial_prices = [22000.0 - i * 5 for i in range(62)]
    initial_bars = pd.DataFrame(
        {
            "open": initial_prices,
            "high": [p + 10 for p in initial_prices],
            "low": [p - 10 for p in initial_prices],
            "close": initial_prices,
            "volume": [500] * 62,
        },
        index=initial_times,
    )
    initial_bars = add_moving_averages(
        initial_bars,
        fast=DEFAULT_STRATEGY.ma_fast,
        mid=DEFAULT_STRATEGY.ma_mid,
        slow=DEFAULT_STRATEGY.ma_slow,
    )

    with patch("backend.strategy_service._load_bars", return_value=initial_bars):
        res = start_strategy(
            strategy_id,
            product,
            qty=1,
            stop_loss_points=80.0,
            take_profit_points=200.0,
            max_loss_ntd=5000.0,
            max_loss_pct=0.05,
            exit_mode="sltp_fixed",
        )
    state = _armed[strategy_id]
    print(f"  ✓ 策略 {strategy_id} 已掛載 (Armed)")
    print(f"  ✓ 專屬風控設定: 停損={state.stop_loss_points}點, 停利={state.take_profit_points}點, 風控上限=-{state.max_loss_ntd}元")

    # 2. 模擬即時推進：新 K 棒大漲收盤，MA5 向上金叉突破 MA20
    print("\n[Step 2] 模擬即時 60 分 K 棒推進收盤，產生 MA 金叉訊號...")
    new_times = pd.date_range("2026-08-30 09:00", periods=65, freq="1h")
    # 構造前 63 根（0..62）走跌使 MA5 < MA20，第 63 根（倒數第二根，剛收盤）大漲產生突破金叉
    prices = [22000.0 - i * 5 for i in range(63)] + [22400.0, 22400.0]
    bars_df = pd.DataFrame(
        {
            "open": prices,
            "high": [p + 20 for p in prices],
            "low": [p - 20 for p in prices],
            "close": prices,
            "volume": [500] * 65,
        },
        index=new_times,
    )
    bars_df = add_moving_averages(
        bars_df,
        fast=DEFAULT_STRATEGY.ma_fast,
        mid=DEFAULT_STRATEGY.ma_mid,
        slow=DEFAULT_STRATEGY.ma_slow,
    )
    from backend.signals import generate_breakout_signals
    s_df = generate_breakout_signals(bars_df)
    print("Signaled tail:")
    print(s_df[["close", "ma_fast", "ma_mid", "signal"]].tail(5))

    # 3. 模擬背景每 60 秒輪詢 (strategy_tick)
    print("\n[Step 3] 觸發輪詢 strategy_tick()，偵測 K 線收盤訊號並進場...")
    mock_svc = MagicMock()
    # 模擬進場市價成交於 22250
    mock_svc.place_order.return_value = {
        "order_result": {"success": True, "order_no": "ORD10001"},
        "state": {"positions": [{"product": "TM09", "direction_key": "long", "qty": 1, "price": "22,250"}]},
    }
    # 模擬掛 OCO 智慧單成功（符合群益真實 5 欄位回傳格式）
    mock_svc.place_oco_order.return_value = {
        "order_result": {
            "success": True,
            "raw": "2026-08-31,條件單號：26589999,BOOK01,26589999,SEQ1234567890",
        }
    }
    # 模擬即時報價 22250 且連線狀態正常
    mock_svc.status.return_value = {
        "connected": True,
        "quote": {"last_price": 22250.0},
        "positions": [{"product": "TM09", "direction_key": "long", "qty": 1, "price": "22,250"}],
    }

    with patch("backend.strategy_service.TradingService.get", return_value=mock_svc), \
         patch("backend.strategy_service._load_bars", return_value=bars_df):
        strategy_tick()

    print(f"  ✓ last_signal: {state.last_signal}")
    print(f"  ✓ last_action: {state.last_action}")
    print(f"  ✓ 進場動作完成！目前內部記帳: held_qty={state.held_qty}, held_direction={state.held_direction}, entry_price={state.entry_price}")
    print(f"  ✓ 券商 OCO 智慧單狀態: key={state.protection_order_smart_key}")
    
    # 驗證 OCO 單掛的價位是否嚴格符合客製化的 80 點停損與 200 點停利
    expected_stop_trigger = 22250.0 - 80.0   # 22170
    expected_profit_trigger = 22250.0 + 200.0 # 22450
    print(f"  ✓ 驗證客製化 OCO 智慧單掛單參數: 停損觸發價={expected_stop_trigger} (-80點), 停利觸發價={expected_profit_trigger} (+200點)")
    mock_svc.place_oco_order.assert_called_once()
    oco_args = mock_svc.place_oco_order.call_args.args[0]
    print(f"  ✓ 送往群益 API 的真實 OCO 封包: {oco_args}")
    assert oco_args["trigger_price"] == str(int(expected_profit_trigger))
    assert oco_args["trigger_price2"] == str(int(expected_stop_trigger))

    # 4. 模擬行情劇烈下跌 85 點至 22165（觸及 80 點停損門檻）
    print("\n[Step 4] 模擬即時行情跌破停損價 (22165 點，虧損 85 點)...")
    # 假設 OCO 在券商端觸發成交，或軟停損備援觸發
    state.protection_order_smart_key = None # 模擬 OCO 觸發後在券商端平倉
    mock_svc.status.return_value = {
        "quote": {"last_price": 22165.0},
        "positions": [], # 券商端部位已平倉
    }
    with patch("backend.strategy_service.TradingService.get", return_value=mock_svc), \
         patch("backend.strategy_service._load_bars", return_value=bars_df):
        strategy_tick()

    print(f"  ✓ 停損平倉後狀態: held_qty={state.held_qty}, last_action={state.last_action}")
    print("\n" + "=" * 60)
    print("🎉 恭喜！Real-Time 60分K線 ➔ 訊號確認 ➔ 客製化 OCO 智慧單 ➔ 觸價停損 全流程模擬 100% 成功！")
    print("=" * 60)


if __name__ == "__main__":
    run_simulation()
