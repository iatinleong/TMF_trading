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


def test_death_cross_uses_its_own_stop_take_and_reverse_settings(tmp_path, monkeypatch):
    from unittest.mock import patch

    from backend import api
    from backend.api import LiveStrategiesBacktestRequest

    tmfr1 = tmp_path / "TMFR1"
    tmfr1.mkdir()
    _write_multi_day(tmfr1, ["2024-07-29", "2024-07-30", "2024-07-31", "2024-08-01", "2024-08-02"])
    monkeypatch.setattr(load_taifex_tick, "RAW_TICK_DIR", tmp_path)

    req = LiveStrategiesBacktestRequest(
        stop_loss_points=100.0,
        take_profit_points=300.0,
        reverse_signal_exit_enabled=True,
        dc_stop_loss_points=55.0,
        dc_take_profit_points=77.0,
        dc_reverse_signal_exit_enabled=True,
    )
    calls = {}
    real = api.run_direction_limited_backtest

    def spy(*args, **kwargs):
        calls.setdefault(kwargs["strategy"], kwargs)
        return real(*args, **kwargs)

    with patch.object(api, "run_direction_limited_backtest", side_effect=spy):
        api.backtest_live_strategies(req)

    dc = calls["death_cross"]
    assert dc["stop_loss_points"] == 55.0
    assert dc["take_profit_points"] == 77.0
    assert dc["reverse_signal_exit_enabled"] is True
    assert calls["breakout"]["stop_loss_points"] == 100.0
    assert calls["breakout"]["take_profit_points"] == 300.0
    assert calls["breakout"]["reverse_signal_exit_enabled"] is True


def test_death_cross_defaults_match_validated_settings():
    from backend.api import LiveStrategiesBacktestRequest

    req = LiveStrategiesBacktestRequest()
    assert req.dc_stop_loss_points == 130.0
    assert req.dc_take_profit_points == 130.0
    assert req.dc_reverse_signal_exit_enabled is False


def test_web_backtest_returns_both_60m_and_15m_klines(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from backend.api import LiveStrategiesBacktestRequest, backtest_live_strategies

    tmfr1 = tmp_path / "TMFR1"
    tmfr1.mkdir()
    _write_multi_day(tmfr1, ["2024-07-29", "2024-07-30", "2024-07-31", "2024-08-01", "2024-08-02"])
    monkeypatch.setattr(load_taifex_tick, "RAW_TICK_DIR", tmp_path)

    body = backtest_live_strategies(LiveStrategiesBacktestRequest())

    assert body["klines"] and body["klines_15"]
    assert len(body["klines_15"]) > len(body["klines"])
    for bar in body["klines_15"]:
        minute = datetime.fromtimestamp(bar["time"], tz=timezone.utc).minute
        assert minute % 15 == 0
