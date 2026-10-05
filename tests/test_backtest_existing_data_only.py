"""回測只能用本機現有的 TMFR1 資料：不能指定日期、不會自動補檔。"""

from unittest.mock import patch

import pandas as pd
import pytest

from backend import load_taifex_tick
from backend.api import LiveStrategiesBacktestRequest


def _write_day(dir_path, day: str) -> None:
    ts = pd.date_range(f"{day} 09:00:00", periods=60, freq="min")
    df = pd.DataFrame({"ts": ts, "close": 20000.0 + pd.RangeIndex(60), "volume": 1})
    df.to_parquet(dir_path / f"TMFR1_{day}.parquet", index=False)


@pytest.fixture
def raw_tick_dir(tmp_path, monkeypatch):
    tmfr1 = tmp_path / "TMFR1"
    tmfr1.mkdir()
    _write_day(tmfr1, "2024-07-29")
    _write_day(tmfr1, "2024-07-30")
    monkeypatch.setattr(load_taifex_tick, "RAW_TICK_DIR", tmp_path)
    return tmp_path


def test_backtest_request_has_no_user_selectable_dates():
    fields = getattr(LiveStrategiesBacktestRequest, "model_fields", None) or LiveStrategiesBacktestRequest.__fields__
    assert "start_date" not in fields
    assert "end_date" not in fields


def test_backtest_loader_reads_all_existing_files_without_dates(raw_tick_dir):
    with patch.object(load_taifex_tick, "ensure_tmfr1_data_available") as mock_ensure:
        bars = load_taifex_tick.load_tmfr1_60min_bars(fetch_missing=False)

    mock_ensure.assert_not_called()
    assert not bars.empty
    dates = sorted({ts.date().isoformat() for ts in bars.index})
    assert dates == ["2024-07-29", "2024-07-30"]


def test_backtest_loader_never_triggers_auto_download_by_default(raw_tick_dir):
    with patch.object(load_taifex_tick, "ensure_tmfr1_data_available", side_effect=AssertionError("should not download")) as mock_ensure:
        bars = load_taifex_tick.load_tmfr1_60min_bars(fetch_missing=False)

    mock_ensure.assert_not_called()
    assert not bars.empty


def _write_multi_day(dir_path, days) -> None:
    import numpy as np

    for day in days:
        ts = pd.date_range(f"{day} 08:45:00", periods=180, freq="min")
        wave = np.sin(np.arange(180) / 6.0) * 40
        df = pd.DataFrame({"ts": ts, "close": 20000.0 + wave, "volume": 1})
        df.to_parquet(dir_path / f"TMFR1_{day}.parquet", index=False)


def test_web_backtest_runs_all_five_strategies_on_existing_data(tmp_path, monkeypatch):
    from backend.api import LiveStrategiesBacktestRequest, backtest_live_strategies

    tmfr1 = tmp_path / "TMFR1"
    tmfr1.mkdir()
    _write_multi_day(tmfr1, ["2024-07-29", "2024-07-30", "2024-07-31", "2024-08-01", "2024-08-02"])
    monkeypatch.setattr(load_taifex_tick, "RAW_TICK_DIR", tmp_path)

    body = backtest_live_strategies(LiveStrategiesBacktestRequest())

    assert set(body["strategies"].keys()) == {
        "breakout_long", "breakout_short", "pullback_long", "pullback_short", "death_cross_short",
    }
