# 死叉做空策略（15分鐘K）Phase 1 回測驗證 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a short-only "MA5/MA20 death cross + MA20 down-slope" strategy on 15-minute bars, validated end-to-end against real TMFR1 historical tick data through the existing backtest engine — no live-trading wiring.

**Architecture:** Reuse the project's existing "signal function → direction-limited backtest engine" pattern (`backend/signals.py` + `backend/backtest_engine.py`) that the 4 live strategies already share. Generalize the existing `resample_to_60min` into a parameterized `resample_to_nmin` for 15-minute bars (pure pandas batch function, no live tick-pipeline changes). Spec: `docs/superpowers/specs/2026-09-19-death-cross-short-backtest-design.md`.

**Tech Stack:** Python, pandas, pytest (existing project stack — no new dependencies).

## Global Constraints

- Signal confirmation on bar close, execution at next bar's open — existing contract in `backend/signals.py`'s module docstring, must not be violated by the new signal function.
- `generate_death_cross_signals` must call `_apply_signal_columns(..., suppress_repeat_same_direction=False)` — NOT the default `True` (verified bug: default silently drops every death-cross signal after the first one, since this strategy never emits a `"long"` signal to reset `last_emitted_signal`).
- `resample_to_60min`'s external behavior must not change (existing live + backtest callers depend on it) — implement as a thin wrapper over the new `resample_to_nmin(df, minutes=60)`.
- No changes to `backend/kline_engine.py` or any live tick-processing path in this plan — Phase 1 is backtest-only.
- Default risk parameters for the new strategy: `stop_loss_points=150.0`, `take_profit_points=150.0`.
- Golden cross while holding a short position does NOT exit early — `reverse_signal_exit_enabled=False` for this strategy. Position only closes via SL/TP/end-of-data.

---

## Task 1: Generalize `resample_to_60min` into `resample_to_nmin`

**Files:**
- Modify: `backend/indicators.py:30-97` (the `resample_to_60min` function)
- Test: `tests/test_indicators.py` (new file)

**Interfaces:**
- Produces: `resample_to_nmin(df: pd.DataFrame, minutes: int) -> pd.DataFrame` — session-aware (day 08:45-13:45 / night 15:00-next day 05:00) resampling into `minutes`-wide buckets. Same day/night splitting rules as the existing function, generalized.
- `resample_to_60min(df: pd.DataFrame) -> pd.DataFrame` keeps its existing signature and becomes a thin wrapper: `return resample_to_nmin(df, minutes=60)`.

- [ ] **Step 1: Write a characterization test locking in `resample_to_60min`'s current behavior**

There is no existing test for this function — write one first so the refactor in Step 3 can't silently change behavior.

```python
# tests/test_indicators.py
import pandas as pd

from backend.indicators import resample_to_60min, resample_to_nmin


def test_resample_to_60min_groups_day_session_ticks_into_hourly_buckets():
    idx = pd.DatetimeIndex([
        "2024-01-02 08:45:00",
        "2024-01-02 09:30:00",
        "2024-01-02 09:45:00",
        "2024-01-02 09:46:00",
        "2024-01-02 10:45:00",
    ])
    df = pd.DataFrame(
        {
            "open": [100, 101, 102, 103, 104],
            "high": [100, 105, 102, 103, 106],
            "low": [100, 99, 102, 103, 102],
            "close": [100, 101, 102, 103, 104],
            "volume": [1, 2, 3, 4, 5],
        },
        index=idx,
    )

    result = resample_to_60min(df)

    assert list(result.index) == [
        pd.Timestamp("2024-01-02 09:45:00"),
        pd.Timestamp("2024-01-02 10:45:00"),
    ]
    first = result.loc[pd.Timestamp("2024-01-02 09:45:00")]
    assert first["open"] == 100
    assert first["close"] == 102
    assert first["high"] == 105
    assert first["low"] == 99
    assert first["volume"] == 6
    second = result.loc[pd.Timestamp("2024-01-02 10:45:00")]
    assert second["open"] == 103
    assert second["close"] == 104
    assert second["volume"] == 9
```

- [ ] **Step 2: Run test to verify it passes against current (pre-refactor) code**

Run: `python -m pytest tests/test_indicators.py -v`
Expected: PASS (this test targets the function's current behavior, before any refactor — it must already pass).

- [ ] **Step 3: Refactor — extract `resample_to_nmin`, make `resample_to_60min` a thin wrapper**

In `backend/indicators.py`, replace the body of `resample_to_60min` (currently lines 30-97) with:

```python
def resample_to_nmin(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Resample intraday Taiwan futures OHLCV data into session-aware N-minute bars.

    The input must contain ``open/high/low/close/volume`` plus either a
    ``DatetimeIndex`` or a ``datetime`` column. Taiwan Index Futures trade in two
    distinct sessions: day (08:45-13:45) and night (15:00-05:00 next day). This
    function resamples each session independently using ``minutes``-wide buckets
    anchored to the session start, so bars never span the 13:45-15:00 break or
    the 05:00-08:45 break, and no bogus non-trading-hour buckets are created.
    Empty bars are dropped automatically. ``minutes`` must evenly divide both
    session lengths (day=300min, night=840min) to avoid a ragged final bucket;
    60 and 15 both divide evenly.
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
    bar_numbers = np.ceil(np.clip(elapsed_minutes, a_min=0, a_max=None) / float(minutes)).astype(int)
    bar_numbers = np.where(bar_numbers == 0, 1, bar_numbers)

    bar_close_times = session_starts.to_numpy() + pd.to_timedelta(bar_numbers * minutes, unit="m")
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


def resample_to_60min(df: pd.DataFrame) -> pd.DataFrame:
    """Session-aware 60-minute resampling. Thin wrapper over ``resample_to_nmin``;
    see that function's docstring for the full session-splitting rules."""
    return resample_to_nmin(df, minutes=60)
```

Note the two lines that changed from the original: `bar_numbers = np.ceil(np.clip(elapsed_minutes, a_min=0, a_max=None) / float(minutes)).astype(int)` (was hardcoded `/ 60.0`) and `bar_close_times = session_starts.to_numpy() + pd.to_timedelta(bar_numbers * minutes, unit="m")` (was hardcoded `* 60`). Everything else is unchanged, just moved into the new function.

- [ ] **Step 4: Run the characterization test again to confirm the refactor didn't change behavior**

Run: `python -m pytest tests/test_indicators.py -v`
Expected: PASS (same test, same result, now going through the new code path).

- [ ] **Step 5: Write the 15-minute bar-count tests**

```python
def test_resample_to_nmin_15_minute_day_session_produces_20_bars():
    idx = pd.date_range("2024-01-02 08:45:00", periods=300, freq="1min")
    df = pd.DataFrame(
        {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1},
        index=idx,
    )

    result = resample_to_nmin(df, minutes=15)

    assert len(result) == 20
    assert result.index[0] == pd.Timestamp("2024-01-02 09:00:00")
    assert result.index[-1] == pd.Timestamp("2024-01-02 13:45:00")


def test_resample_to_nmin_15_minute_night_session_produces_56_bars():
    idx = pd.date_range("2024-01-02 15:00:00", periods=840, freq="1min")
    df = pd.DataFrame(
        {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1},
        index=idx,
    )

    result = resample_to_nmin(df, minutes=15)

    assert len(result) == 56
    assert result.index[0] == pd.Timestamp("2024-01-02 15:15:00")
    assert result.index[-1] == pd.Timestamp("2024-01-03 05:00:00")
```

- [ ] **Step 6: Run all indicators tests**

Run: `python -m pytest tests/test_indicators.py -v`
Expected: 3 passed.

- [ ] **Step 7: Run the full test suite to confirm no regressions elsewhere**

Run: `python -m pytest tests/ -q`
Expected: all tests pass (same count as before plus these 3 new ones).

- [ ] **Step 8: Commit**

```bash
git add backend/indicators.py tests/test_indicators.py
git commit -m "$(cat <<'EOF'
refactor(indicators): generalize resample_to_60min into resample_to_nmin

Extracts the session-aware day/night resampling logic into a parameterized
resample_to_nmin(df, minutes), with resample_to_60min kept as a thin
wrapper so existing callers (live + backtest) are unaffected. Adds a
characterization test for the pre-existing function plus 15-minute bucket
count tests (day=20 bars, night=56 bars), needed for the upcoming
death-cross strategy.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017ykxiou4TVNu3Q1mXgBLVg
EOF
)"
```

---

## Task 2: Add `generate_death_cross_signals`

**Files:**
- Modify: `backend/signals.py` (add new function after `generate_pullback_signals`)
- Test: `tests/test_backtest.py` (existing file — all signal-function tests already live here, follow its conventions)

**Interfaces:**
- Consumes: `_apply_signal_columns` (already in `backend/signals.py`, signature: `_apply_signal_columns(df, *, long_mask, short_mask, include_immediate_reverse=False, suppress_repeat_same_direction=True)`).
- Produces: `generate_death_cross_signals(df: pd.DataFrame) -> pd.DataFrame` — requires `ma_fast`/`ma_mid` columns, returns `df` with `signal`/`signal_color`/`signal_bar_close_time` columns added, same shape as `generate_breakout_signals`/`generate_pullback_signals`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_backtest.py` (append near the other `generate_*_signals` tests, after the existing imports — add `generate_death_cross_signals` to the existing `from backend.signals import ...` line):

```python
def test_generate_death_cross_signals_ignores_cross_when_slope_is_up():
    df = pd.DataFrame({"ma_fast": [12.0, 9.0], "ma_mid": [10.0, 10.5]})

    result = generate_death_cross_signals(df)

    assert result["signal"].isna().all()


def test_generate_death_cross_signals_fires_when_cross_and_slope_down_both_hold():
    df = pd.DataFrame({"ma_fast": [12.0, 9.0], "ma_mid": [10.0, 9.5]})

    result = generate_death_cross_signals(df)

    assert pd.isna(result.iloc[0]["signal"])
    assert result.iloc[1]["signal"] == "short"
    assert result.iloc[1]["signal_color"] == "green"


def test_generate_death_cross_signals_never_emits_long():
    # fast crosses ABOVE mid (a "golden cross" shape) — must never produce "long".
    df = pd.DataFrame({"ma_fast": [8.0, 11.0, 9.0, 12.0], "ma_mid": [10.0, 10.0, 9.5, 9.5]})

    result = generate_death_cross_signals(df)

    assert (result["signal"] != "long").all()


def test_generate_death_cross_signals_fires_on_each_independent_cross_event():
    # Two separate death-cross events with a recovery bar (fast back above mid) between
    # them. This is the regression test for the suppress_repeat_same_direction bug: with
    # the (wrong) default True, the second event would be silently dropped because
    # last_emitted_signal is already "short" and never resets (long_mask is always False).
    df = pd.DataFrame(
        {
            "ma_fast": [12.0, 9.0, 13.0, 9.0],
            "ma_mid": [10.0, 9.5, 9.6, 9.5],
        }
    )

    result = generate_death_cross_signals(df)

    signal_indices = result.index[result["signal"] == "short"].tolist()
    assert signal_indices == [1, 3]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_backtest.py -k death_cross -v`
Expected: FAIL with `ImportError`/`AttributeError: module 'backend.signals' has no attribute 'generate_death_cross_signals'` (or a `NameError` at collection time from the import line if you add the import first — either way, collection/run fails because the function doesn't exist yet).

- [ ] **Step 3: Implement `generate_death_cross_signals`**

Add to `backend/signals.py`, after `generate_pullback_signals`:

```python
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
```

Also update the `backend.signals` import line in `tests/test_backtest.py` to include the new function:

```python
from backend.signals import generate_breakout_signals, generate_death_cross_signals, generate_pullback_signals
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_backtest.py -k death_cross -v`
Expected: 4 passed.

- [ ] **Step 5: Run the full test suite**

Run: `python -m pytest tests/ -q`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add backend/signals.py tests/test_backtest.py
git commit -m "$(cat <<'EOF'
feat(signals): add generate_death_cross_signals (MA5/MA20 + slope filter)

Short-only signal: MA5 death-crosses MA20 AND MA20's own slope is down
(excludes ranging/sideways cross events). suppress_repeat_same_direction
is explicitly False, not the default True — with long_mask always False,
the default would silently drop every death-cross signal after the first
one, since last_emitted_signal never resets. Covered by a dedicated
regression test with two time-separated cross events.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017ykxiou4TVNu3Q1mXgBLVg
EOF
)"
```

---

## Task 3: Wire `"death_cross"` into `backend/backtest_engine.py`

**Files:**
- Modify: `backend/backtest_engine.py:50` (`StrategyName`), `:91` (whitelist), `:178-187` (`_resolve_risk_parameters`), `:173-176` (`_generate_signals`)
- Test: `tests/test_backtest.py`

**Interfaces:**
- Consumes: `generate_death_cross_signals` (Task 2), `BacktestEngine`, `run_direction_limited_backtest` (both already in `backend/backtest_engine.py`).
- Produces: `run_direction_limited_backtest(signaled, strategy="death_cross", direction_limit="short", ...)` works end-to-end; `BacktestEngine(strategy="death_cross", ...)` no longer raises and resolves default risk params to `(150.0, 150.0)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_backtest.py`. First, add these imports at the top of the file (extend the existing import lines):

```python
from backend.backtest_engine import BacktestEngine, run_backtest, run_direction_limited_backtest
from backend.config import DEFAULT_STRATEGY
```

(`DEFAULT_STRATEGY` is new; `ContractSpec, DEFAULT_CONTRACT, DEFAULT_COST, StrategyConfig` stay as already imported from `backend.config`.)

```python
def test_backtest_engine_accepts_death_cross_strategy_with_default_risk_150() -> None:
    engine = BacktestEngine(
        strategy="death_cross",
        contract=_TMF_CONTRACT,
        cost=DEFAULT_COST,
        strategy_cfg=DEFAULT_STRATEGY,
        slippage_points=0.0,
    )

    assert engine.stop_loss_points == pytest.approx(150.0)
    assert engine.take_profit_points == pytest.approx(150.0)


def test_direction_limited_backtest_enters_short_on_death_cross_signal() -> None:
    frame = make_signaled_frame(
        opens=[100.0, 100.0, 95.0, 95.0],
        highs=[100.5, 100.5, 95.5, 95.5],
        lows=[99.5, 99.5, 94.5, 94.5],
        closes=[100.0, 100.0, 95.0, 95.0],
        signals=[None, "short", None, None],
        signal_colors=[None, "green", None, None],
    )

    trades, _ = run_direction_limited_backtest(
        frame,
        strategy="death_cross",
        direction_limit="short",
        contract=_TMF_CONTRACT,
        cost=DEFAULT_COST,
        stop_loss_points=150.0,
        take_profit_points=150.0,
        reverse_signal_exit_enabled=False,
    )

    assert len(trades) == 1
    assert trades[0].direction == "short"
    assert trades[0].entry_price == pytest.approx(95.0)


def test_direction_limited_backtest_death_cross_holds_through_golden_cross_signal() -> None:
    frame = make_signaled_frame(
        opens=[100.0, 100.0, 100.0, 100.0],
        highs=[100.5, 100.5, 100.5, 100.5],
        lows=[99.5, 99.5, 99.5, 99.5],
        closes=[100.0, 100.0, 100.0, 100.0],
        signals=[None, "short", "long", None],
        signal_colors=[None, "green", "red", None],
    )

    trades, _ = run_direction_limited_backtest(
        frame,
        strategy="death_cross",
        direction_limit="short",
        contract=_TMF_CONTRACT,
        cost=DEFAULT_COST,
        stop_loss_points=1000.0,
        take_profit_points=1000.0,
        use_stop_take=True,
        reverse_signal_exit_enabled=False,
    )

    assert len(trades) == 1
    assert trades[0].exit_reason == "end_of_data"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_backtest.py -k death_cross -v`
Expected: the new `BacktestEngine`/`run_direction_limited_backtest` tests FAIL with `ValueError: strategy must be 'breakout' or 'pullback'.`

- [ ] **Step 3: Implement the four wiring changes**

In `backend/backtest_engine.py`:

1. Line 50, change:
```python
StrategyName = Literal["breakout", "pullback"]
```
to:
```python
StrategyName = Literal["breakout", "pullback", "death_cross"]
```

2. Line 91-92, change:
```python
        if strategy not in {"breakout", "pullback"}:
            raise ValueError("strategy must be 'breakout' or 'pullback'.")
```
to:
```python
        if strategy not in {"breakout", "pullback", "death_cross"}:
            raise ValueError("strategy must be 'breakout', 'pullback', or 'death_cross'.")
```

3. Line 173-176, change:
```python
    def _generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        if self.strategy == "breakout":
            return generate_breakout_signals(df)
        return generate_pullback_signals(df)
```
to:
```python
    def _generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        if self.strategy == "breakout":
            return generate_breakout_signals(df)
        if self.strategy == "death_cross":
            return generate_death_cross_signals(df)
        return generate_pullback_signals(df)
```

The signal-function imports live in a try/except fallback block near the top of the file (`backend/backtest_engine.py:26-45`) — both branches need `generate_death_cross_signals` added, not just one:

```python
try:
    from .config import (
        DEFAULT_CONTRACT,
        DEFAULT_COST,
        DEFAULT_STRATEGY,
        ContractSpec,
        CostConfig,
        StrategyConfig,
    )
    from .signals import generate_breakout_signals, generate_death_cross_signals, generate_pullback_signals
except ImportError:  # pragma: no cover - script execution fallback
    from config import (  # type: ignore
        DEFAULT_CONTRACT,
        DEFAULT_COST,
        DEFAULT_STRATEGY,
        ContractSpec,
        CostConfig,
        StrategyConfig,
    )
    from signals import generate_breakout_signals, generate_death_cross_signals, generate_pullback_signals  # type: ignore
```

4. Line 178-187, change:
```python
    def _resolve_risk_parameters(self, strategy: StrategyName) -> tuple[float, float]:
        if strategy == "breakout":
            return (
                float(self.strategy_cfg.breakout_stop_loss_points),
                float(self.strategy_cfg.breakout_take_profit_points),
            )
        return (
            float(self.strategy_cfg.pullback_stop_loss_points),
            float(self.strategy_cfg.pullback_take_profit_points),
        )
```
to:
```python
    def _resolve_risk_parameters(self, strategy: StrategyName) -> tuple[float, float]:
        if strategy == "breakout":
            return (
                float(self.strategy_cfg.breakout_stop_loss_points),
                float(self.strategy_cfg.breakout_take_profit_points),
            )
        if strategy == "death_cross":
            return (150.0, 150.0)
        return (
            float(self.strategy_cfg.pullback_stop_loss_points),
            float(self.strategy_cfg.pullback_take_profit_points),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_backtest.py -k death_cross -v`
Expected: 7 passed (4 from Task 2 + 3 from this task).

- [ ] **Step 5: Run the full test suite**

Run: `python -m pytest tests/ -q`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add backend/backtest_engine.py tests/test_backtest.py
git commit -m "$(cat <<'EOF'
feat(backtest-engine): wire death_cross into StrategyName/whitelist/defaults

Adds "death_cross" to the strategy whitelist, signal-generation dispatch,
and default risk parameters (150/150 pt) so run_direction_limited_backtest
can run the new strategy end-to-end. Confirmed a golden-cross signal while
holding a death_cross short does NOT trigger an early exit when
reverse_signal_exit_enabled=False — the position rides to SL/TP/end_of_data.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017ykxiou4TVNu3Q1mXgBLVg
EOF
)"
```

---

## Task 4: Run the full-history TMF backtest and report results

**Files:**
- Create: `scripts/run_death_cross_backtest.py`

**Interfaces:**
- Consumes: `resample_to_nmin` (Task 1), `generate_death_cross_signals` (Task 2), `run_direction_limited_backtest` with `strategy="death_cross"` (Task 3), `data/raw_tick/TMFR1/*.parquet` (existing data, per-day files named `TMFR1_YYYY-MM-DD.parquet` with columns `ts, close, volume`).
- Produces: console report (trade count, win rate, total P&L, max drawdown, profit factor) — this is a standalone report script, not a library module, so no unit test for it; verification is running it and inspecting the output makes sense.

- [ ] **Step 1: Write the script**

```python
# scripts/run_death_cross_backtest.py
"""執行死叉做空策略（15分鐘K）Phase 1 回測，全量 TMFR1 歷史逐筆資料。

用法：
    python -m scripts.run_death_cross_backtest
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.backtest_engine import run_direction_limited_backtest
from backend.config import ContractSpec, DEFAULT_COST
from backend.indicators import add_moving_averages, resample_to_nmin
from backend.signals import generate_death_cross_signals

TMFR1_DIR = Path(__file__).resolve().parent.parent / "data" / "raw_tick" / "TMFR1"
CONTRACT = ContractSpec(name="TMF", point_value=10.0, tick_size=1.0)


def load_15min_bars() -> pd.DataFrame:
    daily_bars: list[pd.DataFrame] = []
    for path in sorted(TMFR1_DIR.glob("TMFR1_*.parquet")):
        day_df = pd.read_parquet(path, columns=["ts", "close", "volume"])
        day_df = day_df.rename(columns={"ts": "datetime"})
        day_df["open"] = day_df["close"]
        day_df["high"] = day_df["close"]
        day_df["low"] = day_df["close"]
        # 不手動 set_index：_prepare_ohlcv_frame 會自己處理 "datetime" 欄位，
        # 這裡直接沿用 backend/load_taifex_tick.py::load_tmfr1_60min_bars() 已經
        # 驗證過的同一種呼叫方式。
        bars_day = resample_to_nmin(day_df, minutes=15)
        if not bars_day.empty:
            daily_bars.append(bars_day)
    if not daily_bars:
        raise SystemExit(f"找不到任何 TMFR1 逐筆資料於 {TMFR1_DIR}")
    return pd.concat(daily_bars).sort_index(kind="stable")


def main() -> None:
    bars = load_15min_bars()
    print(f"載入 15 分鐘K棒：{len(bars)} 根，區間 {bars.index[0]} ~ {bars.index[-1]}")

    enriched = add_moving_averages(bars, fast=5, mid=20, slow=60)
    signaled = generate_death_cross_signals(enriched)

    trades, summary = run_direction_limited_backtest(
        signaled,
        strategy="death_cross",
        direction_limit="short",
        contract=CONTRACT,
        cost=DEFAULT_COST,
        stop_loss_points=150.0,
        take_profit_points=150.0,
        reverse_signal_exit_enabled=False,
    )

    print(f"\n=== 死叉做空（15分K，SL/TP=150/150）回測結果 ===")
    print(f"總交易數：{summary['total_trades']}")
    print(f"勝率：{summary['win_rate'] * 100:.1f}%")
    print(f"總損益：{summary['total_net_pnl_ntd']:,.0f} 元")
    print(f"最大回撤：{summary['max_drawdown_ntd']:,.0f} 元")
    print(f"獲利因子：{summary['profit_factor']:.2f}")
    print(f"平均獲利：{summary['avg_win_ntd']:,.0f} 元")
    print(f"平均虧損：{summary['avg_loss_ntd']:,.0f} 元")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it against the real TMFR1 dataset**

Run: `python -m scripts.run_death_cross_backtest`
Expected: prints the bar count/date range and a results block with a non-trivial `total_trades` count (data covers 2024-07-29 onward — enough days that death-cross events should occur more than once; if `total_trades` is 0 or 1, treat that as a signal to go back and check the Task 2 regression test actually ran against this code path, not evidence the strategy is simply bad).

- [ ] **Step 3: Sanity-check the printed numbers**

Confirm: `total_trades > 1` (validates the suppress-repeat fix actually took effect against real data, not just the synthetic regression test), win_rate is between 0 and 1 in the summary dict (printed as a 0-100% figure), max_drawdown_ntd is non-negative.

- [ ] **Step 4: Commit**

```bash
git add scripts/run_death_cross_backtest.py
git commit -m "$(cat <<'EOF'
feat(scripts): add death-cross strategy backtest runner (TMFR1, full history)

Loads all TMFR1_*.parquet raw tick files, resamples to 15-minute bars,
runs the death_cross strategy through run_direction_limited_backtest, and
prints win rate / total P&L / max drawdown / profit factor.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017ykxiou4TVNu3Q1mXgBLVg
EOF
)"
```

---

## Explicitly out of scope (per spec, Phase 2 — not part of this plan)

- Live 15-minute tick→bar aggregation pipeline (`backend/kline_engine.py` stays untouched).
- Wiring into `backend/strategy_service.py`'s live arm/disarm, Dashboard toggle, or Supabase `user_strategy_configs` binding.
- TX/MTX deep historical backtests (blocked on raw tick data depth, a separate Shioaji backfill effort).
