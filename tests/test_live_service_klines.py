from unittest.mock import MagicMock, patch

import pytest

from backend import live_service


def test_get_klines_default_uses_60_minute_store(monkeypatch):
    calls = []

    def _fake_get_kline_store(code, interval_minutes=60):
        calls.append(interval_minutes)
        store = MagicMock()
        store.get_klines.return_value = [{"time": 1, "close": 1.0}]
        return store

    monkeypatch.setattr(live_service, "get_kline_store", _fake_get_kline_store)
    monkeypatch.setattr(live_service, "resolve_quote_product_code", lambda p: "TM2609")

    result = live_service.get_klines("TM2609")

    assert calls == [60]
    assert result == [{"time": 1, "close": 1.0}]


def test_get_klines_15min_uses_15_minute_store(monkeypatch):
    calls = []

    def _fake_get_kline_store(code, interval_minutes=60):
        calls.append(interval_minutes)
        store = MagicMock()
        store.get_klines.return_value = [{"time": 2, "close": 2.0}]
        return store

    monkeypatch.setattr(live_service, "get_kline_store", _fake_get_kline_store)
    monkeypatch.setattr(live_service, "resolve_quote_product_code", lambda p: "TM2609")

    result = live_service.get_klines("TM2609", interval_minutes=15)

    assert calls == [15]
    assert result == [{"time": 2, "close": 2.0}]


def _base_status(*, subscribed_product: str) -> dict:
    return {
        "connected": True,
        "subscribed_product": subscribed_product,
        "quote": {"product_code": subscribed_product, "last_price": None},
        "quote_ready": False,
        "quote_connected": 1,
        "kline_loaded_products": [subscribed_product],
    }


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_poll_once_broadcasts_both_60min_and_15min_klines():
    mock_svc = MagicMock()
    mock_svc.status.return_value = _base_status(subscribed_product="TM2610")

    def _fake_get_klines(product, limit=1, interval_minutes=60):
        if interval_minutes == 15:
            return [{"time": 1000, "close": 15.0}]
        return [{"time": 2000, "close": 60.0}]

    with patch("backend.live_service._svc", return_value=mock_svc), \
         patch("backend.strategy_service.resolve_strategy_product_code", return_value="TM2610"), \
         patch("backend.live_service._should_attempt_periodic_shioaji_backfill", return_value=False), \
         patch("backend.live_service.get_klines", side_effect=_fake_get_klines):
        tick = await live_service.poll_once()

    assert tick["kline"]["close"] == 60.0
    assert tick["kline_15m"]["close"] == 15.0
