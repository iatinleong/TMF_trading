"""權益 / K 線解析與 60 分棒對齊單元測試。"""

from datetime import datetime

from backend.broker.capital_futures import (
    _days_for_kline_bars,
    _format_capital_kline_date,
)
from backend.capital_parse import (
    parse_future_rights_raw,
    parse_kline_row,
)
from backend import kline_engine
from backend.kline_engine import DEFAULT_KLINE_LIMIT, LiveKlineStore, bar_close_time_from_ts
from backend.timeutil import to_unix_seconds

import pandas as pd


def test_format_capital_kline_date():
    assert _format_capital_kline_date(datetime(2026, 7, 16)) == "20260716"


def test_days_for_kline_bars_covers_500():
    assert _days_for_kline_bars(500) >= 45


def test_parse_future_rights_raw():
    tokens = [
        "1000000",  # account_balance
        "1500",  # floating_pl
        "100",  # realized_fee
        "0",  # transaction_tax
        "0",  # withheld_premium
        "0",  # premium_payment
        "1015000",  # equity
        "50000",  # excess_margin
        "0",  # deposit_withdrawal
        "0",  # buyer_market_value
        "0",  # seller_market_value
        "-200",  # futures_close_pl
        "1500",  # intraday_unrealized
        "80000",  # initial_margin
        "60000",  # maintenance_margin
    ]
    tokens.extend(["0"] * (41 - len(tokens)))
    tokens[25] = "NTD"
    tokens[34] = "85"
    tokens[39] = "96571013"
    tokens[40] = "F0200006921941"
    raw = ",".join(tokens)
    parsed = parse_future_rights_raw(raw)
    assert parsed["equity"] == 1_015_000.0
    assert parsed["floating_pl"] == 1500.0
    assert parsed["maintenance_margin"] == 60_000.0
    assert parsed["available_balance"] is None or isinstance(parsed.get("available_balance"), (float, type(None)))
    assert parsed["risk_indicator"] == "85"


def test_parse_kline_row_daily():
    row = parse_kline_row("2026/07/07,21500,21600,21450,21550,1234")
    assert row is not None
    assert row["open"] == 21500.0
    assert row["high"] == 21600.0
    assert row["low"] == 21450.0
    assert row["close"] == 21550.0
    assert row["volume"] == 1234


def test_parse_kline_row_intraday():
    row = parse_kline_row("2026/07/07,09:45,21500,21600,21450,21550,500")
    assert row is not None
    assert row["close"] == 21550.0
    assert row["volume"] == 500
    assert isinstance(row["time"], int)


def test_parse_kline_row_intraday_datetime_same_field():
    """群益實盤分 K：日期時間同欄（逗號後才是 OHLC）。"""
    row = parse_kline_row("2026/07/01 02:00, 47372.00, 47459.00, 47355.00, 47434.00, 1049")
    assert row is not None
    assert row["open"] == 47372.0
    assert row["high"] == 47459.0
    assert row["low"] == 47355.0
    assert row["close"] == 47434.0
    assert row["volume"] == 1049
    assert row["time"] == to_unix_seconds("2026-07-01 02:00:00")


def test_live_kline_store_persists_closed_bar_across_restart(tmp_path, monkeypatch):
    """
    2026-08-25 實測抓到的資料缺口：群益 RequestKLineAMByDate 回補要整個交易
    時段真正收完盤才會補齊那個時段的資料——時段進行中重開後端，重開前已經
    收盤、但時段還沒結束的那幾根 K 棒，回補 API 拿不到，重開後也沒有即時
    tick 可以重建，會永久消失。這裡驗證：只要曾經真的收盤過一次（下一筆
    tick 開出新的一根），LiveKlineStore 落地存檔過，重開後（=重新建立一個
    全新的 LiveKlineStore 實例，沒有任何記憶）要讀得回那根已經收盤的資料。
    """
    monkeypatch.setattr(kline_engine, "_CACHE_DIR", tmp_path)

    store = LiveKlineStore("TESTCACHE")
    # 09:00 台灣時間（日盤內）：開出第一根（收盤標籤 09:45）
    store.on_tick(100.0, 1, pd.Timestamp("2026-08-25 09:00:00"))
    # 09:50：跨進下一根（收盤標籤 10:45），代表 09:45 那根已經收盤定案
    store.on_tick(101.0, 1, pd.Timestamp("2026-08-25 09:50:00"))

    # 模擬後端重開：重新建立一個全新的 LiveKlineStore（沒有任何即時 tick 記憶）
    restarted_store = LiveKlineStore("TESTCACHE")
    times = [b["time"] for b in restarted_store.get_klines()]

    closed_bar_key = to_unix_seconds(pd.Timestamp("2026-08-25 09:45:00"))
    assert closed_bar_key in times, "重開後應該還讀得回已經收盤的那根 K 棒"

    # 還在成型中的那根（10:45）本來就還沒收盤定案，重開後遺失是預期行為，
    # 不是這個修復要解決的問題——這裡只確認它不會誤把未收盤的資料當成已收盤存檔。
    open_bar_key = to_unix_seconds(pd.Timestamp("2026-08-25 10:45:00"))
    assert open_bar_key not in times


def test_bar_close_time_from_ts_day_session():
    ts = pd.Timestamp("2026-07-07 10:30:00")
    close = bar_close_time_from_ts(ts)
    assert close == pd.Timestamp("2026-07-07 10:45:00")


def test_bar_close_time_from_ts_night_session():
    ts = pd.Timestamp("2026-07-07 16:30:00")
    close = bar_close_time_from_ts(ts)
    assert close == pd.Timestamp("2026-07-07 17:00:00")


def test_live_kline_store_starts_empty_without_csv():
    store = LiveKlineStore("TMF00")
    assert store.get_klines() == []


def test_live_kline_store_trims_to_default_limit():
    store = LiveKlineStore("TMF00")
    for i in range(DEFAULT_KLINE_LIMIT + 20):
        store._bars[i] = {
            "time": i,
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "volume": 1,
        }
    store._trim_bars()
    assert len(store.get_klines(limit=0)) == DEFAULT_KLINE_LIMIT
    assert store.get_klines(limit=1)[0]["time"] == DEFAULT_KLINE_LIMIT + 19


def test_live_kline_store_on_tick():
    store = LiveKlineStore("TMFR1")
    ts = pd.Timestamp("2026-07-07 10:30:00")
    store.on_tick(21500.0, 10, ts)
    bar = store.latest_bar()
    assert bar is not None
    assert bar["open"] == 21500.0
    assert bar["close"] == 21500.0
    store.on_tick(21550.0, 5, ts)
    bar = store.latest_bar()
    assert bar["high"] == 21550.0
    assert bar["close"] == 21550.0
    assert bar["volume"] == 15