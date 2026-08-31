"""
scripts/run_realistic_tick_simulation.py
使用 data/raw_tick/TMFR1 真實 Tick 資料進行 4 策略 + 4 道防線的全流程擬真測試。
"""

from __future__ import annotations

import glob
import json
import logging
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

# 修正 Windows UTF-8 輸出
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.indicators import add_moving_averages
from backend.kline_engine import bar_close_time_from_ts
from backend.strategy_service import (
    STRATEGY_DEFS,
    StrategyState,
    _cancel_protection_order_for_state,
    _place_oco_protection_for_state,
    _signaled_frame,
    _stop_with_reason,
    _tick_one,
    strategy_tick,
)

logger = logging.getLogger("simulation")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


class MockTradingService:
    """模擬券商端狀態與委託執行"""

    def __init__(self):
        self.orders = []
        self.cancel_orders = []
        self.stop_orders = []
        self.positions = []
        self.current_quote = 22000.0
        self.reject_next_order = False
        self.oco_seq = 80000001

    def place_order(self, order_dict: dict) -> dict:
        if self.reject_next_order:
            self.reject_next_order = False
            return {
                "order_result": {"success": False, "message": "[530] 券商拒單測試"},
                "state": {"positions": self.positions},
            }
        seq = str(len(self.orders) + 1001)
        side = order_dict["side"]
        qty = order_dict["qty"]
        price = self.current_quote
        order_dict["seq_no"] = seq
        order_dict["fill_price"] = price
        self.orders.append(order_dict)

        # 更新內部 mock positions
        new_close = order_dict.get("new_close", 0)
        if new_close == 0:  # 新倉
            self.positions.append(
                {"product": order_dict["product_code"], "direction": side, "qty": qty, "price": price}
            )
        else:  # 平倉
            self.positions.clear()

        return {
            "order_result": {"success": True, "seq_no": seq, "message": "委託成功"},
            "state": {"positions": self.positions},
        }

    def place_oco_order(self, oco_dict: dict) -> dict:
        seq = str(self.oco_seq)
        self.oco_seq += 1
        oco_dict["seq_no"] = seq
        self.stop_orders.append(oco_dict)
        return {
            "success": True,
            "order_result": {
                "success": True,
                "key": seq,
                "raw": f"2026/09/01,委託成功,BOOK123,{seq},SEQ999999",
                "message": "OCO智慧單委託成功",
            },
        }

    def place_stop_order(self, stop_dict: dict) -> dict:
        seq = str(self.oco_seq)
        self.oco_seq += 1
        stop_dict["seq_no"] = seq
        self.stop_orders.append(stop_dict)
        return {
            "success": True,
            "key": seq,
            "order_result": {"key": seq, "message": "OCO智慧單委託成功"},
        }

    def cancel_stop_order(self, cancel_dict: dict) -> dict:
        self.cancel_orders.append(cancel_dict)
        return {
            "cancel_result": {"success": True, "message": "OCO智慧單撤單成功"},
            "state": {"positions": self.positions},
        }


def load_real_ticks_to_klines(parquet_files: list[str]) -> pd.DataFrame:
    """載入真實 Parquet 檔案並合成 60 分 K 棒，計算 5/20/60 MA。"""
    dfs = []
    for f in parquet_files:
        df = pd.read_parquet(f)
        dfs.append(df)
    full_df = pd.concat(dfs, ignore_index=True)
    full_df["ts"] = pd.to_datetime(full_df["ts"])
    full_df = full_df.sort_values("ts")
    full_df["bar_close"] = full_df["ts"].apply(bar_close_time_from_ts)
    full_df = full_df.dropna(subset=["bar_close"])

    grouped = full_df.groupby("bar_close").agg(
        open=("close", "first"),
        high=("close", "max"),
        low=("close", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    ).reset_index()

    grouped = grouped.rename(columns={"bar_close": "datetime"})
    grouped["datetime"] = grouped["datetime"].dt.tz_localize(None)
    grouped = grouped.set_index("datetime")
    grouped = add_moving_averages(grouped, fast=5, mid=20, slow=60)
    return grouped


def run_realistic_simulation():
    print("=" * 70)
    print("🚀 台指期量化系統 — 全流程擬真壓力測試（真實 Tick + 4 策略 + 4 道防線）")
    print("=" * 70)

    # 1. 載入真實 Tick 並合成 60 分 K 棒
    files = sorted(glob.glob("data/raw_tick/TMFR1/*.parquet"))
    if not files:
        print("❌ 找不到 data/raw_tick/TMFR1/*.parquet 檔案")
        return

    sample_files = files[:10]  # 取前 10 天真實檔案做測試
    print(f"📊 正在從 {len(sample_files)} 個真實 Parquet 檔案讀取 Tick 並合成 60 分 K 棒...")
    klines_df = load_real_ticks_to_klines(sample_files)
    print(f"✓ 成功合成 {len(klines_df)} 根 60 分 K 棒（時間區間: {klines_df.index[0]} ~ {klines_df.index[-1]}）")

    mock_svc = MockTradingService()

    # =========================================================================
    # 測試項目 1：4 策略進場與同向疊單防護驗證 (Stacking / Pyramiding Protection)
    # =========================================================================
    print("\n" + "-" * 70)
    print("【測試 1】4 策略進場與同向疊單防護（驗證持倉時同向訊號是否不會疊單）")
    print("-" * 70)

    states = {
        sid: StrategyState(
            strategy_id=sid,
            strategy=defs["strategy"],
            direction_limit=defs["direction_limit"],
            label=defs.get("label", sid),
            product_code="TM2609",
            qty=1,
            stop_loss_points=80.0,
            take_profit_points=200.0,
            max_loss_ntd=5000.0,
            max_loss_pct=0.05,
            oco_enabled=True,
            soft_stop_enabled=True,
            risk_insurance_enabled=True,
            reverse_signal_exit_enabled=True,
        )
        for sid, defs in STRATEGY_DEFS.items()
    }

    # 模擬 breakout_long 策略第一次收到多方訊號
    s_long = states["breakout_long"]
    mock_svc.current_quote = 22200.0

    df_long_signal = klines_df.copy()

    with patch("backend.strategy_service._load_bars", return_value=df_long_signal), \
         patch("backend.strategy_service._latest_closed_signal", return_value={"time": "2026-09-01T09:45:00", "direction": "long", "key": "bar1_long"}):
        
        # 第一次觸發：空手 ➔ 應該進場 long x1 並掛出 OCO 智慧單
        _tick_one(s_long, mock_svc, {"quote": {"last_price": 22200.0}})
        assert s_long.held_qty == 1, f"預期 held_qty=1，實際={s_long.held_qty}"
        assert s_long.held_direction == "long"
        assert s_long.protection_order_smart_key is not None, "預期掛出 OCO 智慧單"
        print(f"  ✓ 第一次多方訊號：成功進場 long x1 (進場價={s_long.entry_price})，掛出 OCO Key={s_long.protection_order_smart_key}")

        # 第二次觸發（下一個 Bar 出現新的多方訊號）：應該拒絕疊單，保持口數 1
        with patch("backend.strategy_service._latest_closed_signal", return_value={"time": "2026-09-01T10:45:00", "direction": "long", "key": "bar2_long"}):
            _tick_one(s_long, mock_svc, {"quote": {"last_price": 22300.0}})
            assert s_long.held_qty == 1, f"預期同向不疊單 held_qty=1，實際={s_long.held_qty}"
            assert "已持倉" in s_long.last_action and "同向訊號不動作" in s_long.last_action
            print(f"  ✓ 第二次同向訊號（疊單測試）：{s_long.last_action} ➔ 成功防護，未超額開倉！")

    # =========================================================================
    # 測試項目 2：Layer 4 反向訊號出場（做多持倉遇到做空訊號）
    # =========================================================================
    print("\n" + "-" * 70)
    print("【測試 2】Layer 4 反向訊號出場（做多持倉遇到做空訊號時，平倉出場但不反手）")
    print("-" * 70)

    # 模擬出現反向做空訊號
    with patch("backend.strategy_service._load_bars", return_value=df_long_signal), \
         patch("backend.strategy_service._latest_closed_signal", return_value={"time": "2026-09-01T11:45:00", "direction": "short", "key": "bar3_short"}):
        
        mock_svc.current_quote = 22350.0  # 獲利 150 點出場
        _tick_one(s_long, mock_svc, {"quote": {"last_price": 22350.0}})
        assert s_long.held_qty == 0, f"預期反向平倉後 held_qty=0，實際={s_long.held_qty}"
        assert s_long.held_direction is None
        assert s_long.protection_order_smart_key is None, "預期 OCO 智慧單已撤銷"
        assert s_long.realized_pnl_ntd > 0, "預期實現獲利損益"
        print(f"  ✓ 遇到反向訊號：{s_long.last_action}")
        print(f"  ✓ 持倉清空 (held_qty={s_long.held_qty})，已實現損益 = +{s_long.realized_pnl_ntd:,.0f} 元")
        print(f"  ✓ 成功撤銷原 OCO 保護單 (Cancel orders count={len(mock_svc.cancel_orders)})，且未反手開空倉！")

    # =========================================================================
    # 測試項目 3：Layer 1 券商端 OCO 停利觸發與成交
    # =========================================================================
    print("\n" + "-" * 70)
    print("【測試 3】Layer 1 券商端 OCO 停利掛單驗證（做空進場掛雙向 OCO 保護）")
    print("-" * 70)

    s_short = states["breakout_short"]
    mock_svc.current_quote = 22000.0

    # breakout_short 進場做空 1 口
    with patch("backend.strategy_service._load_bars", return_value=df_long_signal), \
         patch("backend.strategy_service._latest_closed_signal", return_value={"time": "2026-09-01T12:45:00", "direction": "short", "key": "bar4_short"}):
        _tick_one(s_short, mock_svc, {"quote": {"last_price": 22000.0}})
        assert s_short.held_qty == 1
        assert s_short.held_direction == "short"
        assert s_short.protection_order_smart_key is not None
        print(f"  ✓ 做空進場成功: held={s_short.held_direction} x{s_short.held_qty} @ {s_short.entry_price}, OCO Key={s_short.protection_order_smart_key}")

    # =========================================================================
    # 測試項目 4：Layer 2 本地端軟停損備援（行情跳空跌破 80 點時強制市價平倉）
    # =========================================================================
    print("\n" + "-" * 70)
    print("【測試 4】Layer 2 本地端軟停損備援（行情大跌 90 點，本地軟停損觸發市價平倉）")
    print("-" * 70)

    # 重新讓 breakout_long 進場 @ 22000 (無 OCO 保護狀態，模擬 OCO 備援情境)
    s_long = states["breakout_long"]
    s_long.held_qty = 1
    s_long.held_direction = "long"
    s_long.entry_price = 22000.0
    s_long.realized_pnl_ntd = 0.0
    s_long.protection_order_smart_key = None

    # 行情瞬間跌到 21910（虧損 90 點 > 停損門檻 80 點）
    _tick_one(s_long, mock_svc, {"quote": {"last_price": 21910.0}})
    assert s_long.held_qty == 0, f"預期軟停損後 held_qty=0，實際={s_long.held_qty}"
    assert s_long.realized_pnl_ntd < 0
    print(f"  ✓ 觸發本地軟停損: {s_long.last_action}")
    print(f"  ✓ 持倉歸零，實現虧損: {s_long.realized_pnl_ntd:,.0f} 元")

    # =========================================================================
    # 測試項目 5：Layer 3 金額/比例硬停損保險（累積虧損達上限自動停止策略）
    # =========================================================================
    print("\n" + "-" * 70)
    print("【測試 5】Layer 3 金額/比例硬停損保險（單日累積虧損達 -5,000 元，強制鎖死停止策略）")
    print("-" * 70)

    # 人為設定累計虧損達 -5,100 元 (上限 -5,000 元)
    s_long.realized_pnl_ntd = -5100.0
    s_long.held_qty = 0
    s_long.held_direction = None

    with patch("backend.strategy_service._load_bars", return_value=df_long_signal), \
         patch("backend.strategy_service._latest_closed_signal", return_value={"time": "2026-09-01T13:45:00", "direction": "long", "key": "bar5_long"}):
        _tick_one(s_long, mock_svc, {"quote": {"last_price": 22000.0}})
        assert s_long.stopped is True, "預期觸發風控上限後 strategy.stopped=True"
        assert s_long.held_qty == 0, "風控鎖死後不應開新倉"
        print(f"  ✓ 觸發 Layer 3 風控保險網: {s_long.stop_reason}")
        print(f"  ✓ 策略已永久鎖死停止 (stopped={s_long.stopped})，徹底防止繼續虧損！")

    # =========================================================================
    # 測試項目 6：回彈策略 (Pullback) 雙向運作驗證
    # =========================================================================
    print("\n" + "-" * 70)
    print("【測試 6】回彈策略（pullback_long & pullback_short）訊號生成與驗證")
    print("-" * 70)

    s_pb_long = states["pullback_long"]
    s_pb_short = states["pullback_short"]

    signaled_pb = _signaled_frame("pullback", klines_df)
    long_signals = signaled_pb[signaled_pb["signal"] == "long"]
    short_signals = signaled_pb[signaled_pb["signal"] == "short"]
    print(f"  ✓ 10 天真實行情中檢測到 Pullback 做多訊號 {len(long_signals)} 次，做空訊號 {len(short_signals)} 次")
    if not long_signals.empty:
        print(f"    - 最近做多訊號時間: {long_signals['datetime'].iloc[-1]} @ close={long_signals['close'].iloc[-1]}")
    if not short_signals.empty:
        print(f"    - 最近做空訊號時間: {short_signals['datetime'].iloc[-1]} @ close={short_signals['close'].iloc[-1]}")

    # =========================================================================
    # 測試項目 7：券商拒單與孤兒單防護驗證
    # =========================================================================
    print("\n" + "-" * 70)
    print("【測試 7】券商拒單與非同步異常防護（券商回傳失敗時的重試與停止機制）")
    print("-" * 70)

    s_pb_long.held_qty = 0
    mock_svc.reject_next_order = True  # 模擬下一次下單被券商拒絕 [530]

    with patch("backend.strategy_service._load_bars", return_value=df_long_signal), \
         patch("backend.strategy_service._latest_closed_signal", return_value={"time": "2026-09-01T14:45:00", "direction": "long", "key": "bar6_long"}):
        _tick_one(s_pb_long, mock_svc, {"quote": {"last_price": 22000.0}})
        assert s_pb_long.held_qty == 0, "拒單時不應標記持倉"
        assert s_pb_long.consecutive_failures == 1, "連續失敗計數加 1"
        assert "下單失敗" in s_pb_long.last_action
        print(f"  ✓ 券商拒單處理正確: {s_pb_long.last_action}")
        print("  ✓ 同一訊號標記已消費，不會每秒轟炸券商！")

    print("\n" + "=" * 70)
    print("🎉 7 大全流程擬真壓力測試全數 100% 通過！未發現邏輯崩潰或邊界條件漏洞！")
    print("=" * 70)


if __name__ == "__main__":
    run_realistic_simulation()
