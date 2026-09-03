from __future__ import annotations

from datetime import time

import numpy as np
import pandas as pd

_OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")


def _prepare_ohlcv_frame(df: pd.DataFrame) -> pd.DataFrame:
    if "datetime" in df.columns:
        prepared = df.copy()
        prepared["datetime"] = pd.to_datetime(prepared["datetime"])
        prepared = prepared.set_index("datetime")
    else:
        if not isinstance(df.index, pd.DatetimeIndex):
            raise TypeError("df must have a DatetimeIndex or a 'datetime' column.")
        prepared = df.copy()

    missing_columns = [column for column in _OHLCV_COLUMNS if column not in prepared.columns]
    if missing_columns:
        raise ValueError(f"Missing required OHLCV columns: {missing_columns}")

    prepared.index = pd.to_datetime(prepared.index)
    prepared = prepared.sort_index(kind="stable")
    return prepared


def resample_to_60min(df: pd.DataFrame) -> pd.DataFrame:
    """Resample intraday Taiwan futures OHLCV data into session-aware 60-minute bars.

    The input must contain ``open/high/low/close/volume`` plus either a
    ``DatetimeIndex`` or a ``datetime`` column. Taiwan Index Futures trade in two
    distinct sessions: day (08:45-13:45) and night (15:00-05:00 next day). This
    function resamples each session independently, using 60-minute buckets
    equivalent to ``label='right', closed='right'`` anchored to the session start
    (day bars ending 09:45, 10:45, ..., 13:45; night bars ending 16:00, 17:00,
    ..., 05:00).

    Because the day and night sessions are split before aggregation, bars never
    span the 13:45-15:00 break or the 05:00-08:45 break, and no bogus
    non-trading-hour buckets are created. Empty bars are dropped automatically.
    """

    prepared = _prepare_ohlcv_frame(df)

    timestamps = prepared.index
    times = timestamps.time

    is_day = (times >= time(8, 45)) & (times <= time(13, 45))
    is_night_late = times >= time(15, 0)
    is_night_early = times <= time(5, 0)
    is_night = is_night_late | is_night_early
    is_trading = is_day | is_night

    session_df = prepared.loc[is_trading, list(_OHLCV_COLUMNS)].copy()
    if session_df.empty:
        return session_df

    session_index = session_df.index
    session_times = session_index.time
    session_type = np.where(
        (session_times >= time(8, 45)) & (session_times <= time(13, 45)),
        "day",
        "night",
    )

    trade_dates = session_index.normalize()
    night_early_mask = session_times <= time(5, 0)
    session_anchor_date = trade_dates.where(~night_early_mask, trade_dates - pd.Timedelta(days=1))

    session_starts = pd.Series(session_anchor_date, index=session_index)
    day_mask = session_type == "day"
    session_starts.loc[day_mask] = session_starts.loc[day_mask] + pd.Timedelta(hours=8, minutes=45)
    session_starts.loc[~day_mask] = session_starts.loc[~day_mask] + pd.Timedelta(hours=15)

    elapsed_minutes = (session_index - session_starts.to_numpy()) / pd.Timedelta(minutes=1)
    bar_numbers = np.ceil(np.clip(elapsed_minutes, a_min=0, a_max=None) / 60.0).astype(int)
    bar_numbers = np.where(bar_numbers == 0, 1, bar_numbers)

    bar_close_times = session_starts.to_numpy() + pd.to_timedelta(bar_numbers * 60, unit="m")
    session_df["_bar_close_time"] = pd.DatetimeIndex(bar_close_times)

    resampled = (
        session_df.groupby("_bar_close_time", sort=True)
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
        .dropna(how="all")
    )
    resampled.index.name = "datetime"
    return resampled


def add_moving_averages(df: pd.DataFrame, fast: int, mid: int, slow: int) -> pd.DataFrame:
    """Add trailing simple moving averages on the already-closed ``close`` prices.

    ``ma_fast``, ``ma_mid``, and ``ma_slow`` are computed with the standard
    trailing rolling mean on the ``close`` column. A bar's MA value therefore uses
    only data up to and including that bar's own close, which is correct because
    the bar is already closed at that timestamp.

    Important for future maintainers: these MA values become *known* only when the
    bar closes. Strategy code must therefore consume bar ``t``'s MA state no
    earlier than bar ``t+1``'s open to avoid look-ahead bias. Enforcement belongs
    in signal/execution code, not in this indicator helper.
    """

    if "close" not in df.columns:
        raise ValueError("df must contain a 'close' column.")

    enriched = df.copy()
    enriched["ma_fast"] = enriched["close"].rolling(window=fast).mean()
    enriched["ma_mid"] = enriched["close"].rolling(window=mid).mean()
    enriched["ma_slow"] = enriched["close"].rolling(window=slow).mean()
    return enriched
