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


def test_lunar_new_year_holiday_does_not_split():
    # 2025 春節：1/22 最後一根，2/3 才恢復，間隔 12 天，是休市不是資料缺口
    first = _bars("2025-01-21 09:45", 20)
    second = _bars("2025-02-03 09:45", 20)
    assert len(split_at_data_gaps(pd.concat([first, second]))) == 1


def test_real_data_gap_splits():
    # 本機資料 2025-12-16 之後到 2026-05-12 沒有任何檔案，應該切段
    first = _bars("2025-12-15 09:45", 20)
    second = _bars("2026-05-12 09:45", 20)
    assert len(split_at_data_gaps(pd.concat([first, second]))) == 2


def test_empty_input_gives_no_segments():
    assert split_at_data_gaps(pd.DataFrame()) == []


def test_enrich_marks_gap_and_restarts_indicators_per_segment():
    from backend.segmented_backtest import enrich_by_segments

    first = _bars("2024-07-29 09:45", 80)
    second = _bars("2026-05-12 09:45", 80)
    enriched = enrich_by_segments(pd.concat([first, second]))

    assert enriched["gap_after"].sum() == 1
    assert bool(enriched["gap_after"].iloc[79]) is True
    # 第二段的慢線要從第二段自己的第 60 根才有值，不能拿第一段的價格來算
    assert enriched["ma_slow"].iloc[80:139].isna().all()
    assert enriched["ma_slow"].iloc[139:].notna().all()
