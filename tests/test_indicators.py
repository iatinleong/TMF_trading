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
