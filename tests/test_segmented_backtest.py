"""按資料缺口切段回測的單元測試。"""

import pandas as pd

from backend.segmented_backtest import split_at_data_gaps


def _bars(start: str, periods: int) -> pd.DataFrame:
    index = pd.date_range(start, periods=periods, freq="60min")
    return pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1}, index=index)


def test_splits_when_gap_exceeds_threshold():
    first = _bars("2024-07-29 09:45", 50)
    second = _bars("2024-12-17 09:45", 50)
    segments = split_at_data_gaps(pd.concat([first, second]))

    assert len(segments) == 2
    assert segments[0].index.max() < segments[1].index.min()
    assert len(segments[0]) == 50 and len(segments[1]) == 50


def test_no_split_for_contiguous_data():
    bars = _bars("2024-07-29 09:45", 50)
    assert len(split_at_data_gaps(bars)) == 1


def test_short_holiday_gap_does_not_split():
    first = _bars("2025-01-20 09:45", 20)
    second = _bars("2025-01-28 09:45", 20)  # 春節前後約一週休市，不視為資料缺口
    assert len(split_at_data_gaps(pd.concat([first, second]))) == 1


def test_empty_input_gives_no_segments():
    assert split_at_data_gaps(pd.DataFrame()) == []
