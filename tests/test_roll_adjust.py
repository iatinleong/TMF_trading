"""換月價差調整（back-adjust）測試。"""

import pandas as pd

from backend.roll_adjust import back_adjust, find_roll_points, third_wednesdays


def _bars(rows: list[tuple[str, float]]) -> pd.DataFrame:
    index = pd.DatetimeIndex([pd.Timestamp(ts) for ts, _ in rows], name="datetime")
    closes = [close for _, close in rows]
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": [1] * len(rows)},
        index=index,
    )


def _two_sided_day(settlement: str, old_close: float, new_open: float) -> pd.DataFrame:
    """結算日：日盤最後一根（13:45）收舊合約價，夜盤第一根（15:15）開新合約價。"""
    return _bars(
        [
            (f"{settlement} 13:30", old_close),
            (f"{settlement} 13:45", old_close),
            (f"{settlement} 15:15", new_open),
            (f"{settlement} 15:30", new_open),
        ]
    )


def test_third_wednesday_dates():
    days = third_wednesdays([2026])
    assert days[0] == pd.Timestamp("2026-01-21")
    assert days[2] == pd.Timestamp("2026-03-18")
    assert all(day.weekday() == 2 for day in days)


def test_roll_point_delta_is_new_open_minus_old_close():
    bars = _two_sided_day("2026-03-18", old_close=33800.0, new_open=33650.0)
    rolls = find_roll_points(bars, [pd.Timestamp("2026-03-18")])
    assert len(rolls) == 1
    roll_time, delta = rolls[0]
    assert roll_time == pd.Timestamp("2026-03-18 15:15")
    assert delta == -150.0


def test_settlement_without_both_sessions_is_skipped_not_guessed():
    bars = _bars([("2026-03-18 09:00", 100.0), ("2026-03-18 09:15", 101.0)])
    assert find_roll_points(bars, [pd.Timestamp("2026-03-18")]) == []


def test_back_adjust_makes_series_continuous_at_roll():
    old_part = _bars([("2026-03-17 10:00", 100.0), ("2026-03-18 13:45", 110.0)])
    new_part = _bars([("2026-03-18 15:15", 80.0), ("2026-03-19 10:00", 85.0)])
    bars = pd.concat([old_part, new_part])
    rolls = find_roll_points(bars, [pd.Timestamp("2026-03-18")])
    adjusted = back_adjust(bars, rolls)

    before = adjusted.loc[adjusted.index < pd.Timestamp("2026-03-18 15:15"), "close"].iloc[-1]
    after = adjusted.loc[pd.Timestamp("2026-03-18 15:15"), "open"]
    assert before == after  # 換月處接平


def test_back_adjust_keeps_moves_within_each_contract():
    old_part = _bars([("2026-03-17 10:00", 100.0), ("2026-03-18 13:45", 110.0)])
    new_part = _bars([("2026-03-18 15:15", 80.0), ("2026-03-19 10:00", 95.0)])
    bars = pd.concat([old_part, new_part])
    adjusted = back_adjust(bars, find_roll_points(bars, [pd.Timestamp("2026-03-18")]))

    assert adjusted.loc["2026-03-19 10:00", "close"] - adjusted.loc["2026-03-18 15:15", "close"] == 15.0
    assert adjusted.loc["2026-03-18 13:45", "close"] - adjusted.loc["2026-03-17 10:00", "close"] == 10.0


def test_volume_is_not_adjusted():
    bars = _two_sided_day("2026-03-18", old_close=110.0, new_open=80.0)
    adjusted = back_adjust(bars, find_roll_points(bars, [pd.Timestamp("2026-03-18")]))
    assert (adjusted["volume"] == bars["volume"]).all()


def test_moving_average_has_no_artificial_cross_after_adjustment():
    # 結算日 08:45 到 13:45 為舊合約（每 15 分鐘一根，共 21 根，價 100），15:00 起為新合約（價 80）
    old_times = pd.date_range("2026-03-18 08:45", "2026-03-18 13:45", freq="15min")
    new_times = pd.date_range("2026-03-18 15:00", periods=25, freq="15min")
    rows = [(str(t), 100.0) for t in old_times] + [(str(t), 80.0) for t in new_times]
    bars = _bars(rows)

    raw_ma = bars["close"].rolling(20).mean()
    adjusted = back_adjust(bars, find_roll_points(bars, [pd.Timestamp("2026-03-18")]))
    adj_ma = adjusted["close"].rolling(20).mean()

    raw_jump = (raw_ma.diff().abs()).max()
    adj_jump = (adj_ma.diff().abs()).max()
    assert adj_jump < raw_jump
