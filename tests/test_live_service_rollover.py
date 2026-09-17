"""backend/live_service.py::poll_once() 的近月合約自動換月測試。

2026-09-17 實盤事故：TM2609 結算到期（每月第三個星期三）之後，SKCOM 訂閱、
儀表板都還停在這個舊代碼上，圖表/K線一片空白。根因有兩層：
1. 換月判斷原本寫在 strategy_service._tick_one() 裡，只有「有策略被武裝」
   時才會執行到——使用者把策略全部停用之後，永遠不會被觸發。
2. 就算執行到了，也只是改 StrategyState 自己的 product_code 欄位，從來沒有
   真的呼叫 TradingService.subscribe() 讓 SKCOM 連線層跟著換合約。

這裡測的是修好之後、獨立於任何策略是否啟動的 poll_once() 換月邏輯。
"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _base_status(*, subscribed_product: str) -> dict:
    return {
        "connected": True,
        "subscribed_product": subscribed_product,
        "quote": {"product_code": subscribed_product, "last_price": None},
        "quote_ready": False,
        "quote_connected": 1,
        "kline_loaded_products": [subscribed_product],
    }


@pytest.mark.anyio
async def test_poll_once_resubscribes_when_subscribed_product_is_expired():
    """訂閱的商品是過期代碼（TM2609）、當前近月已經是 TM2610 時，
    應該自動呼叫 set_product 重新訂閱新合約，不依賴任何策略是否啟動。"""
    mock_svc = MagicMock()
    mock_svc.status.return_value = _base_status(subscribed_product="TM2609")

    with patch("backend.live_service._svc", return_value=mock_svc), \
         patch("backend.strategy_service.resolve_strategy_product_code", return_value="TM2610"), \
         patch("backend.live_service.set_product") as mock_set_product, \
         patch("backend.live_service._should_attempt_periodic_shioaji_backfill", return_value=False), \
         patch("backend.live_service.get_klines", return_value=[]):
        from backend.live_service import poll_once

        await poll_once()

    mock_set_product.assert_called_once_with("TM2610")


@pytest.mark.anyio
async def test_poll_once_does_not_resubscribe_when_product_is_current():
    """訂閱的商品本來就是當前近月合約時，不應該多此一舉重新訂閱。"""
    mock_svc = MagicMock()
    mock_svc.status.return_value = _base_status(subscribed_product="TM2610")

    with patch("backend.live_service._svc", return_value=mock_svc), \
         patch("backend.strategy_service.resolve_strategy_product_code", return_value="TM2610"), \
         patch("backend.live_service.set_product") as mock_set_product, \
         patch("backend.live_service._should_attempt_periodic_shioaji_backfill", return_value=False), \
         patch("backend.live_service.get_klines", return_value=[]):
        from backend.live_service import poll_once

        await poll_once()

    mock_set_product.assert_not_called()


@pytest.mark.anyio
async def test_poll_once_survives_resolve_failure_without_crashing():
    """換月判斷本身如果意外拋出例外，不該讓整個 poll_once() 掛掉——
    只是這一輪不換月，其餘輪詢照常進行。"""
    mock_svc = MagicMock()
    mock_svc.status.return_value = _base_status(subscribed_product="TM2609")

    with patch("backend.live_service._svc", return_value=mock_svc), \
         patch("backend.strategy_service.resolve_strategy_product_code", side_effect=RuntimeError("boom")), \
         patch("backend.live_service.set_product") as mock_set_product, \
         patch("backend.live_service._should_attempt_periodic_shioaji_backfill", return_value=False), \
         patch("backend.live_service.get_klines", return_value=[]):
        from backend.live_service import poll_once

        result = await poll_once()

    mock_set_product.assert_not_called()
    assert result is not None
    assert result["type"] == "tick"


@pytest.mark.anyio
async def test_poll_once_survives_resubscribe_failure_without_crashing():
    """換月判斷正確偵測到需要換月，但實際重新訂閱失敗（例如券商暫時拒絕）時，
    不該讓整個 poll_once() 掛掉。"""
    mock_svc = MagicMock()
    mock_svc.status.return_value = _base_status(subscribed_product="TM2609")

    with patch("backend.live_service._svc", return_value=mock_svc), \
         patch("backend.strategy_service.resolve_strategy_product_code", return_value="TM2610"), \
         patch("backend.live_service.set_product", side_effect=RuntimeError("subscribe failed")), \
         patch("backend.live_service._should_attempt_periodic_shioaji_backfill", return_value=False), \
         patch("backend.live_service.get_klines", return_value=[]):
        from backend.live_service import poll_once

        result = await poll_once()

    assert result is not None
    assert result["type"] == "tick"
