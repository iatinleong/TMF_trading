from __future__ import annotations

"""
Signal-generation contract for the backtest engine:

1. A signal on timestamp ``t`` is confirmed only at bar ``t``'s close.
2. The execution layer must therefore enter/reverse no earlier than bar
   ``t+1``'s open. Never trade on bar ``t``'s own open/close from these signals.
3. Breakout signals are emitted in alternating direction only; repeated
   same-direction breakout setups are suppressed until the opposite crossover
   appears. Pullback signals keep the opposite-direction immediate-reverse flag,
   but may emit fresh same-direction touch events again later.
"""

import numpy as np
import pandas as pd


def _validate_columns(df: pd.DataFrame, required_columns: list[str]) -> None:
    missing_columns = [column for column in required_columns if column not in df.columns]
    if missing_columns:
        raise ValueError(f"Missing required columns: {missing_columns}")


def _signal_bar_close_time(index: pd.Index) -> pd.Series:
    return pd.Series(pd.to_datetime(index), index=index, name="signal_bar_close_time")


def _apply_signal_columns(
    df: pd.DataFrame,
    *,
    long_mask: pd.Series,
    short_mask: pd.Series,
    include_immediate_reverse: bool = False,
    suppress_repeat_same_direction: bool = True,
) -> pd.DataFrame:
    if (long_mask & short_mask).any():
        raise ValueError("A bar cannot be both a long and short signal.")

    enriched = df.copy()
    candidate_signals = np.full(len(enriched), None, dtype=object)

    long_values = long_mask.fillna(False).to_numpy(dtype=bool)
    short_values = short_mask.fillna(False).to_numpy(dtype=bool)
    candidate_signals[long_values] = "long"
    candidate_signals[short_values] = "short"

    emitted_signals = np.full(len(enriched), None, dtype=object)
    immediate_reverse = np.zeros(len(enriched), dtype=bool)
    last_emitted_signal: str | None = None

    for index, candidate in enumerate(candidate_signals):
        if candidate is None:
            continue
        if suppress_repeat_same_direction and candidate == last_emitted_signal:
            continue

        emitted_signals[index] = candidate
        immediate_reverse[index] = last_emitted_signal is not None and candidate != last_emitted_signal
        last_emitted_signal = candidate

    signal_color = np.where(
        emitted_signals == "long",
        "red",
        np.where(emitted_signals == "short", "green", None),
    )

    enriched["signal"] = pd.Series(emitted_signals, index=enriched.index, dtype="object")
    enriched["signal_color"] = pd.Series(signal_color, index=enriched.index, dtype="object")
    enriched["signal_bar_close_time"] = _signal_bar_close_time(enriched.index)
    if include_immediate_reverse:
        enriched["immediate_reverse"] = immediate_reverse
    return enriched


def generate_breakout_signals(df: pd.DataFrame) -> pd.DataFrame:
    """Generate confirmed 60-minute breakout signals from precomputed fast/mid MAs.

    Long signals are confirmed when ``ma_fast`` crosses from ``<= ma_mid`` to
    ``> ma_mid`` by the current bar's close. Short signals are confirmed on the
    opposite cross. These signals are informational only: the backtest engine must
    execute them at the next bar's open using ``signal_bar_close_time`` as the
    confirmation timestamp.
    """

    _validate_columns(df, ["ma_fast", "ma_mid"])

    previous_fast = df["ma_fast"].shift(1)
    previous_mid = df["ma_mid"].shift(1)

    long_mask = previous_fast.le(previous_mid) & df["ma_fast"].gt(df["ma_mid"])
    short_mask = previous_fast.ge(previous_mid) & df["ma_fast"].lt(df["ma_mid"])

    return _apply_signal_columns(df, long_mask=long_mask, short_mask=short_mask)


def generate_pullback_signals(df: pd.DataFrame) -> pd.DataFrame:
    """Generate confirmed pullback/rebound signals from 20MA/60MA regime rules.

    This is a concrete implementation of the informal trading rule:
    ``多方回測60k60ma+60k20ma之上時 回測60k20ma 出現作多訊號`` /
    ``空方回測60k60ma+60k20ma之下時 回彈60k20ma 出現作空訊號``.

    We operationalize that wording as follows:
    - Bullish regime: ``close > ma_slow`` and price remains above the 20MA by the
      current close. A long signal is confirmed when the previous bar's low stayed
      above its 20MA, then the current bar dips into/touches the current 20MA
      (``low <= ma_mid``) and closes back at/above it (``ma_mid <= close``).
    - Bearish regime: ``close < ma_slow`` and price remains below the 20MA by the
      current close. A short signal is confirmed when the previous bar's high
      stayed below its 20MA, then the current bar rallies into/touches the current
      20MA (``high >= ma_mid``) and closes back at/below it
      (``ma_mid >= close``).

    As with breakout signals, confirmation happens only at the current bar's
    close; execution must occur at the next bar's open. Pullback touches remain
    edge-triggered, but unlike breakout they may emit repeated same-direction
    signals after later fresh touches. ``immediate_reverse`` is still marked
    ``True`` whenever an emitted signal is opposite to the last emitted signal,
    which lets the execution layer implement close-and-reverse behavior.
    """

    _validate_columns(df, ["close", "low", "high", "ma_mid", "ma_slow"])

    previous_low = df["low"].shift(1)
    previous_high = df["high"].shift(1)
    previous_mid = df["ma_mid"].shift(1)

    bullish_regime = df["close"].gt(df["ma_slow"]) & df["close"].ge(df["ma_mid"])
    bearish_regime = df["close"].lt(df["ma_slow"]) & df["close"].le(df["ma_mid"])

    long_mask = (
        bullish_regime
        & previous_low.gt(previous_mid)
        & df["low"].le(df["ma_mid"])
        & df["ma_mid"].le(df["close"])
    )
    short_mask = (
        bearish_regime
        & previous_high.lt(previous_mid)
        & df["high"].ge(df["ma_mid"])
        & df["ma_mid"].ge(df["close"])
    )

    return _apply_signal_columns(
        df,
        long_mask=long_mask,
        short_mask=short_mask,
        include_immediate_reverse=True,
        suppress_repeat_same_direction=False,
    )


def generate_death_cross_signals(df: pd.DataFrame) -> pd.DataFrame:
    """Generate confirmed short-only death-cross signals from precomputed MA5/MA20.

    A short signal is confirmed when ``ma_fast`` crosses from ``>= ma_mid`` to
    ``< ma_mid`` by the current bar's close (death cross), AND ``ma_mid`` itself
    is lower than the previous bar's ``ma_mid`` (20MA slope is down) — the slope
    filter excludes cross events that happen during sideways/ranging conditions.
    This strategy never emits a long signal; a golden cross (opposite crossover)
    produces no signal at all, it does not close-and-reverse.

    As with the other signal functions, confirmation happens only at the current
    bar's close; execution must occur at the next bar's open.
    """

    _validate_columns(df, ["ma_fast", "ma_mid"])

    previous_fast = df["ma_fast"].shift(1)
    previous_mid = df["ma_mid"].shift(1)

    cross_down = previous_fast.ge(previous_mid) & df["ma_fast"].lt(df["ma_mid"])
    slope_down = df["ma_mid"].lt(previous_mid)

    short_mask = cross_down & slope_down
    long_mask = pd.Series(False, index=df.index)

    return _apply_signal_columns(
        df,
        long_mask=long_mask,
        short_mask=short_mask,
        suppress_repeat_same_direction=False,
    )
