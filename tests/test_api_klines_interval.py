from unittest.mock import patch

from backend.api import live_klines, live_klines_signals


def test_live_klines_default_interval_is_60():
    with patch("backend.strategy_service.resolve_strategy_product_code", return_value="TM2609"), \
         patch("backend.api.get_klines", return_value=[]) as mock_get_klines:
        live_klines(product="TMF", limit=500)

    assert mock_get_klines.call_args.kwargs["interval_minutes"] == 60


def test_live_klines_passes_interval_15_to_get_klines():
    with patch("backend.strategy_service.resolve_strategy_product_code", return_value="TM2609"), \
         patch("backend.api.get_klines", return_value=[]) as mock_get_klines:
        live_klines(product="TMF", limit=500, interval=15)

    assert mock_get_klines.call_args.kwargs["interval_minutes"] == 15


def test_live_klines_signals_passes_interval_15_to_klines_with_signals():
    with patch("backend.strategy_service.resolve_strategy_product_code", return_value="TM2609"), \
         patch("backend.api.klines_with_signals", return_value=[]) as mock_kws:
        live_klines_signals(product="TMF", limit=500, interval=15)

    assert mock_kws.call_args.kwargs["interval_minutes"] == 15
