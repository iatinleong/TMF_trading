"""backend/shioaji_backfill.py 的單元測試：缺口偵測邏輯、交易日查詢日期對照、
以及 LiveKlineStore.merge_missing_bars 的不覆蓋語意。不測試真的連線 Shioaji
（需要真實金鑰/網路，屬於整合測試範疇，見 2026-08-25 的手動驗證紀錄）。
"""

from unittest.mock import patch

import pandas as pd

from backend import kline_engine, shioaji_backfill
from backend.kline_engine import LiveKlineStore, get_store
from backend.shioaji_backfill import (
    _expected_day_session_labels,
    _expected_night_session_labels,
    _session_query_dates,
    backfill_via_shioaji,
    find_missing_bars,
    shioaji_available,
)


def test_backfill_via_shioaji_logs_why_it_skipped_when_no_keys(tmp_path, monkeypatch):
    """
    2026-08-26：VM 上回報「好像沒補成功」，但完全查不出是哪一步失敗——這個
    模組的 logger.info/warning 在一般部署下不會輸出到任何看得到的地方。這裡
    驗證跳過補缺時，至少會落地留一筆看得懂原因的稽核紀錄，不是完全沉默。
    """
    audit_path = tmp_path / "shioaji_backfill_audit.log"
    monkeypatch.setattr(shioaji_backfill, "_audit_log_path", lambda: audit_path)
    monkeypatch.setattr(shioaji_backfill, "shioaji_available", lambda: False)

    added = backfill_via_shioaji("TESTAUDITSKIP")

    assert added == 0
    content = audit_path.read_text(encoding="utf-8")
    assert "TESTAUDITSKIP" in content
    assert "沒有可用的 SJ_API_KEY/SJ_SEC_KEY" in content


def test_backfill_via_shioaji_logs_login_failure(tmp_path, monkeypatch):
    audit_path = tmp_path / "shioaji_backfill_audit.log"
    monkeypatch.setattr(shioaji_backfill, "_audit_log_path", lambda: audit_path)
    monkeypatch.setattr(shioaji_backfill, "shioaji_available", lambda: True)
    monkeypatch.setattr(shioaji_backfill, "_is_production", lambda: True)
    monkeypatch.setattr(
        shioaji_backfill, "find_missing_bars", lambda *a, **k: [("2026-08-26", pd.Timestamp("2026-08-26 09:45"))]
    )

    class _FakeApi:
        def login(self, **kwargs):
            raise RuntimeError("模擬登入失敗")

        def logout(self):
            pass

    fake_module = type("FakeShioaji", (), {"Shioaji": lambda self, simulation: _FakeApi()})()

    with patch.dict("sys.modules", {"shioaji": fake_module}):
        added = backfill_via_shioaji("TESTAUDITLOGIN")

    assert added == 0
    content = audit_path.read_text(encoding="utf-8")
    assert "login 失敗" in content
    assert "模擬登入失敗" in content


def test_shioaji_available_picks_up_supabase_sourced_keys(monkeypatch):
    """
    2026-08-26 實測抓到的缺口：SJ_API_KEY/SJ_SEC_KEY 改成集中放 Supabase 之後
    （本機 .env 不再存這兩個值），shioaji_available() 原本只呼叫 load_dotenv()
    讀本機 .env，完全不知道要去 Supabase 抓，一律誤判成「沒設定金鑰」，導致
    補缺功能永遠不會被觸發——即使 Supabase 那邊的密鑰其實是好的。這裡驗證
    shioaji_available() 現在會透過 backend.secrets_store.load_remote_secrets()
    正確套用遠端密鑰。
    """
    monkeypatch.delenv("SJ_API_KEY", raising=False)
    monkeypatch.delenv("SJ_SEC_KEY", raising=False)

    def _fake_remote_fetch() -> int:
        monkeypatch.setenv("SJ_API_KEY", "remote_key")
        monkeypatch.setenv("SJ_SEC_KEY", "remote_secret")
        return 2

    with patch("backend.shioaji_backfill.load_remote_secrets", side_effect=_fake_remote_fetch):
        assert shioaji_available() is True
from backend.timeutil import to_unix_seconds


def test_expected_day_session_labels():
    labels = _expected_day_session_labels(pd.Timestamp("2026-08-25"))
    assert [t.strftime("%H:%M") for t in labels] == [
        "09:45",
        "10:45",
        "11:45",
        "12:45",
        "13:45",
    ]


def test_expected_night_session_labels():
    labels = _expected_night_session_labels(pd.Timestamp("2026-08-25"))
    assert len(labels) == 14
    assert labels[0] == pd.Timestamp("2026-08-25 16:00")
    assert labels[-1] == pd.Timestamp("2026-08-26 05:00")


def test_session_query_dates_night_session_queries_next_day():
    """
    2026-08-25 實測驗證：Shioaji ticks(date=X) 的 X 涵蓋「X 當天日盤 +
    X 前一天夜盤」，所以某天的夜盤（15:00~次日05:00）要用「隔天」日期查。
    這個對照關係錯了，補的資料時間就全部錯位，必須用回歸測試釘住。
    """
    pairs = _session_query_dates(pd.Timestamp("2026-08-25"))
    day_pairs = [(q, label) for q, label in pairs if label.strftime("%H:%M") == "09:45"]
    night_pairs = [(q, label) for q, label in pairs if label.strftime("%H:%M") == "16:00"]

    assert day_pairs[0][0] == "2026-08-25"
    assert night_pairs[0][0] == "2026-08-26"


def test_session_query_dates_excludes_weekends():
    """週末（週六與週日）沒有當日開盤的日盤與夜盤，不應產生查詢日期，避免誤判缺口。"""
    saturday_pairs = _session_query_dates(pd.Timestamp("2026-08-29"))  # 週六
    sunday_pairs = _session_query_dates(pd.Timestamp("2026-08-30"))    # 週日

    assert saturday_pairs == []
    assert sunday_pairs == []


def test_bar_close_time_from_ts_excludes_weekends():
    """驗證週六早上 05:00 之後與週日全天，bar_close_time_from_ts 均回傳 None。"""
    from backend.kline_engine import bar_close_time_from_ts

    # 週五夜盤在週六 05:00 收盤（合法）
    assert bar_close_time_from_ts(pd.Timestamp("2026-08-29 04:30:00")) == pd.Timestamp("2026-08-29 05:00:00")

    # 週六 05:00 以後（休市）
    assert bar_close_time_from_ts(pd.Timestamp("2026-08-29 05:01:00")) is None
    assert bar_close_time_from_ts(pd.Timestamp("2026-08-29 17:00:00")) is None

    # 週日全天（休市）
    assert bar_close_time_from_ts(pd.Timestamp("2026-08-30 00:00:00")) is None
    assert bar_close_time_from_ts(pd.Timestamp("2026-08-30 11:45:00")) is None

    # 週一開盤前 08:45 以前（休市）
    assert bar_close_time_from_ts(pd.Timestamp("2026-08-31 08:00:00")) is None

    # 週一 08:45 開盤後（合法）
    assert bar_close_time_from_ts(pd.Timestamp("2026-08-31 08:46:00")) == pd.Timestamp("2026-08-31 09:45:00")


def test_find_missing_bars_detects_gap_in_closed_session(monkeypatch):
    """
    只放 5 天前那個交易日日盤的其中 4 根（缺 11:45 那根），驗證能準確抓出
    「已經收盤、真的缺的那一根」，不會把整個時段當成缺，也不會誤報其他
    已經有資料的根。
    """
    product = "TESTGAPDETECT"
    store = get_store(product)
    store._bars.clear()

    base_day = pd.Timestamp.now().normalize() - pd.Timedelta(days=5)
    all_labels = _expected_day_session_labels(base_day)
    present_labels = [label for label in all_labels if label.strftime("%H:%M") != "11:45"]
    for label in present_labels:
        key = to_unix_seconds(label)
        store._bars[key] = {
            "time": key, "open": 100.0, "high": 100.0, "low": 100.0,
            "close": 100.0, "volume": 1,
        }

    missing = find_missing_bars(product, lookback_days=6)
    missing_labels_on_base_day = [
        label for _, label in missing
        if label.normalize() == base_day and label.strftime("%H:%M") in
        [t.strftime("%H:%M") for t in all_labels]
    ]
    assert len(missing_labels_on_base_day) == 1
    assert missing_labels_on_base_day[0].strftime("%H:%M") == "11:45"


def test_find_missing_bars_ignores_future_bars():
    """還沒收盤（未來）的時間點不該被當成缺口，不然每次啟動都會被判定『全缺』。"""
    product = "TESTGAPFUTURE"
    store = get_store(product)
    store._bars.clear()

    missing = find_missing_bars(product, lookback_days=1)
    now = pd.Timestamp.now()
    assert all(label < now for _, label in missing)


def test_merge_missing_bars_does_not_overwrite_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(kline_engine, "_CACHE_DIR", tmp_path)
    store = LiveKlineStore("TESTMERGE")
    store._bars[1000] = {
        "time": 1000, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1,
    }

    added = store.merge_missing_bars(
        [
            {"time": 1000, "open": 999.0, "high": 999.0, "low": 999.0, "close": 999.0, "volume": 999},
            {"time": 2000, "open": 2.0, "high": 2.0, "low": 2.0, "close": 2.0, "volume": 2},
        ]
    )

    assert added == 1
    assert store._bars[1000]["close"] == 1.0  # 既有資料沒被覆蓋
    assert store._bars[2000]["close"] == 2.0  # 真的缺的補上了


def test_resample_ticks_to_bars_sorts_unsorted_ticks():
    """驗證 Shioaji 回傳亂序 tick 時，_resample_ticks_to_bars 會先按時間排序，確保 open/close 正確。"""
    from backend.shioaji_backfill import _resample_ticks_to_bars

    # 構造 10:00 的 tick，故意把 10:15 的 tick 放前面（close=21600），10:01 的放後面（close=21500）
    df = pd.DataFrame(
        [
            {"ts": "2026-08-25 10:15:00", "close": 21600.0, "volume": 10},
            {"ts": "2026-08-25 10:01:00", "close": 21500.0, "volume": 5},
            {"ts": "2026-08-25 10:40:00", "close": 21700.0, "volume": 20},
        ]
    )
    bars = _resample_ticks_to_bars(df)
    assert len(bars) == 1
    # 排序後 10:01 是 open (21500)，10:40 是 close (21700)
    assert bars[0]["open"] == 21500.0
    assert bars[0]["close"] == 21700.0
    assert bars[0]["high"] == 21700.0
    assert bars[0]["low"] == 21500.0
    assert bars[0]["volume"] == 35
