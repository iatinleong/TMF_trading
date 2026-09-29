import pandas as pd

from backend.kline_engine import LiveKlineStore, bar_close_time_from_ts, get_kline_store, get_store


def test_bar_close_time_from_ts_default_60min_matches_hourly_bucket():
    ts = pd.Timestamp("2024-01-02 09:30:00")
    assert bar_close_time_from_ts(ts) == pd.Timestamp("2024-01-02 09:45:00")


def test_bar_close_time_from_ts_15min_buckets_correctly():
    ts = pd.Timestamp("2024-01-02 09:05:00")
    assert bar_close_time_from_ts(ts, minutes=15) == pd.Timestamp("2024-01-02 09:15:00")
    # boundary tick exactly on a bucket edge stays in that bucket, not the next one
    ts_edge = pd.Timestamp("2024-01-02 09:15:00")
    assert bar_close_time_from_ts(ts_edge, minutes=15) == pd.Timestamp("2024-01-02 09:15:00")


def test_live_kline_store_default_interval_is_60_minutes(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.kline_engine._CACHE_DIR", tmp_path)
    store = LiveKlineStore("TM_TEST_60")
    store.on_tick(100.0, 1, pd.Timestamp("2024-01-02 09:00:00"))
    store.on_tick(101.0, 1, pd.Timestamp("2024-01-02 09:30:00"))
    bars = store.get_klines()
    assert len(bars) == 1
    assert bars[0]["close"] == 101.0


def test_live_kline_store_15min_interval_creates_separate_bars(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.kline_engine._CACHE_DIR", tmp_path)
    store = LiveKlineStore("TM_TEST_15", interval_minutes=15)
    store.on_tick(100.0, 1, pd.Timestamp("2024-01-02 09:00:00"))
    store.on_tick(101.0, 1, pd.Timestamp("2024-01-02 09:20:00"))
    bars = store.get_klines()
    assert len(bars) == 2


def test_get_store_is_a_thin_wrapper_over_get_kline_store_60min(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.kline_engine._CACHE_DIR", tmp_path)
    a = get_store("TM_TEST_WRAP")
    b = get_kline_store("TM_TEST_WRAP", interval_minutes=60)
    assert a is b


def test_get_kline_store_15min_is_a_separate_store_from_60min(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.kline_engine._CACHE_DIR", tmp_path)
    a = get_store("TM_TEST_SEP")
    b = get_kline_store("TM_TEST_SEP", interval_minutes=15)
    assert a is not b
    assert b.interval_minutes == 15
    assert a.interval_minutes == 60
