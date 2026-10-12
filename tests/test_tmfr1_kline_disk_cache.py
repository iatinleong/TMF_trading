"""TMFR1 每日 K 棒磁盤快取的增量行為測試。

用合成的逐筆檔（不碰真實資料）驗證：
- 第一次建立快取、之後新行程直接讀快取（不重算任何一天）
- 只有內容或修改時間變了的那一天會重算
- 新增檔案只重算新檔，刪除檔案會從結果與快取中移除
- 快取版本號不符時整批重算
"""

import pandas as pd
import pytest

from backend import load_taifex_tick as loader


def _write_day(tmfr1_dir, date_str: str, close_base: float) -> None:
    ts = pd.date_range(f"{date_str} 09:00:00", periods=10, freq="min")
    frame = pd.DataFrame({"ts": ts, "close": [close_base + i for i in range(10)], "volume": [1] * 10})
    frame.to_parquet(tmfr1_dir / f"TMFR1_{date_str}.parquet")


@pytest.fixture
def tick_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    tmfr1_dir = data_dir / "raw_tick" / "TMFR1"
    tmfr1_dir.mkdir(parents=True)
    monkeypatch.setattr(loader, "DATA_DIR", data_dir)
    monkeypatch.setattr(loader, "RAW_TICK_DIR", data_dir / "raw_tick")
    loader._TMFR1_BARS_CACHE.clear()

    recomputed: list[str] = []
    real_resample = loader._resample_daily_file

    def counting_resample(path, minutes):
        recomputed.append(path.name)
        return real_resample(path, minutes)

    monkeypatch.setattr(loader, "_resample_daily_file", counting_resample)
    return tmfr1_dir, recomputed


def _fresh_process_load(minutes: int = 60) -> pd.DataFrame:
    # 模擬新行程：清掉行程內快取，只剩磁盤快取可用
    loader._TMFR1_BARS_CACHE.clear()
    return loader.load_tmfr1_60min_bars(fetch_missing=False, minutes=minutes)


def test_second_fresh_load_reads_disk_cache_without_recomputing(tick_env):
    tmfr1_dir, recomputed = tick_env
    _write_day(tmfr1_dir, "2026-07-07", 100.0)
    _write_day(tmfr1_dir, "2026-07-08", 200.0)

    first = _fresh_process_load()
    assert sorted(recomputed) == ["TMFR1_2026-07-07.parquet", "TMFR1_2026-07-08.parquet"]

    recomputed.clear()
    second = _fresh_process_load()
    assert recomputed == []
    assert first.equals(second)


def test_only_changed_day_is_recomputed(tick_env):
    tmfr1_dir, recomputed = tick_env
    _write_day(tmfr1_dir, "2026-07-07", 100.0)
    _write_day(tmfr1_dir, "2026-07-08", 200.0)
    _fresh_process_load()

    recomputed.clear()
    _write_day(tmfr1_dir, "2026-07-08", 300.0)  # 內容改了，修改時間也跟著變
    result = _fresh_process_load()

    assert recomputed == ["TMFR1_2026-07-08.parquet"]
    assert result["close"].max() == pytest.approx(309.0)


def test_added_day_is_computed_and_existing_days_reused(tick_env):
    tmfr1_dir, recomputed = tick_env
    _write_day(tmfr1_dir, "2026-07-07", 100.0)
    _fresh_process_load()

    recomputed.clear()
    _write_day(tmfr1_dir, "2026-07-09", 400.0)
    result = _fresh_process_load()

    assert recomputed == ["TMFR1_2026-07-09.parquet"]
    assert set(result.index.date) == {pd.Timestamp("2026-07-07").date(), pd.Timestamp("2026-07-09").date()}


def test_deleted_day_is_removed_from_result_and_cache(tick_env):
    tmfr1_dir, _ = tick_env
    _write_day(tmfr1_dir, "2026-07-07", 100.0)
    _write_day(tmfr1_dir, "2026-07-08", 200.0)
    _fresh_process_load()

    (tmfr1_dir / "TMFR1_2026-07-08.parquet").unlink()
    result = _fresh_process_load()

    assert set(result.index.date) == {pd.Timestamp("2026-07-07").date()}
    _, manifest_path = loader._kline_cache_paths(60)
    assert "TMFR1_2026-07-08.parquet" not in manifest_path.read_text(encoding="utf-8")


def test_version_mismatch_forces_full_recompute(tick_env, monkeypatch):
    tmfr1_dir, recomputed = tick_env
    _write_day(tmfr1_dir, "2026-07-07", 100.0)
    _write_day(tmfr1_dir, "2026-07-08", 200.0)
    _fresh_process_load()

    recomputed.clear()
    monkeypatch.setattr(loader, "_KLINE_CACHE_VERSION", loader._KLINE_CACHE_VERSION + 1)
    _fresh_process_load()

    assert sorted(recomputed) == ["TMFR1_2026-07-07.parquet", "TMFR1_2026-07-08.parquet"]


def test_disk_cache_is_separate_per_bar_size(tick_env):
    tmfr1_dir, _ = tick_env
    _write_day(tmfr1_dir, "2026-07-07", 100.0)
    bars_60 = _fresh_process_load(minutes=60)
    bars_15 = _fresh_process_load(minutes=15)

    assert len(bars_15) > len(bars_60)
    assert loader._kline_cache_paths(60)[0] != loader._kline_cache_paths(15)[0]
