"""按資料缺口切段回測。

本機 TMFR1 資料有幾個月的缺口（例如 2025-12 到 2026-05）。如果直接把前後兩段接在一起，
均線會跨過缺口，訊號和交易都會被污染。這裡先按缺口切段，每段各自算均線、訊號與交易，
最後把交易合併、按進場時間排序，再算一次績效。
"""

from __future__ import annotations

from collections.abc import Callable

import pandas as pd

from .backtest_engine import Trade, build_summary, run_direction_limited_backtest
from .config import DEFAULT_STRATEGY
from .indicators import add_moving_averages

DATA_GAP_THRESHOLD = pd.Timedelta(days=10)


def split_at_data_gaps(bars: pd.DataFrame, max_gap: pd.Timedelta = DATA_GAP_THRESHOLD) -> list[pd.DataFrame]:
    if bars.empty:
        return []
    ordered = bars.sort_index()
    gap_starts = ordered.index.to_series().diff() > max_gap
    segment_ids = gap_starts.cumsum().to_numpy()
    return [ordered[segment_ids == seg_id] for seg_id in sorted(set(segment_ids))]


def run_segmented_backtest(
    raw_bars: pd.DataFrame,
    signal_fn: Callable[[pd.DataFrame], pd.DataFrame],
    *,
    strategy: str,
    direction_limit: str,
    **engine_kwargs,
) -> tuple[list[Trade], dict]:
    trades: list[Trade] = []
    for segment in split_at_data_gaps(raw_bars):
        enriched = add_moving_averages(
            segment, fast=DEFAULT_STRATEGY.ma_fast, mid=DEFAULT_STRATEGY.ma_mid, slow=DEFAULT_STRATEGY.ma_slow,
        )
        enriched.index = pd.to_datetime(enriched.index)
        enriched = enriched.sort_index()
        signaled = signal_fn(enriched)
        segment_trades, _ = run_direction_limited_backtest(
            signaled, strategy=strategy, direction_limit=direction_limit, **engine_kwargs,
        )
        trades.extend(segment_trades)
    trades.sort(key=lambda trade: trade.entry_time)
    return trades, build_summary(trades)
