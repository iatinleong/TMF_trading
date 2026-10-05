"""backend/live_service.py::set_product() 切換商品時觸發 Shioaji 補缺的測試。

2026-10-01 實測回報：換月後切到新合約（例如 TM2610），MA60 一直不出現——
根因是 Shioaji 補缺本來要等 300 秒的定期節流才會跑，而群益自己的歷史回補
不保證湊得到 60 根。這裡測的是 set_product() 新增的「切換當下立刻額外補一次」
邏輯，重點是 lookback_days 的安全上限（TMFR1 是近月連續代碼，往回查太遠會
查到換月前舊合約的報價、誤標成新合約污染資料）。
"""

from unittest.mock import MagicMock, patch


def _mock_svc(connected: bool = True) -> MagicMock:
    svc = MagicMock()
    svc.status.return_value = {"connected": connected}
    return svc


def test_set_product_triggers_shioaji_backfill_capped_by_days_since_rollover():
    """微台（TM開頭)商品：lookback_days 取 min(5, 距離上次換月的天數)，
    不是無條件用 5 天——這裡模擬換月才過 2 天的情境，驗證真的有被限制住。"""
    with patch("backend.live_service._svc", return_value=_mock_svc()), \
         patch("backend.live_service.resolve_quote_product_code", return_value="TM2610"), \
         patch("backend.live_service._is_worker_mode", return_value=False), \
         patch("backend.trading_service.days_since_tmf_rollover", return_value=2) as mock_days, \
         patch("backend.shioaji_backfill.backfill_via_shioaji") as mock_backfill:
        from backend.live_service import set_product

        set_product("TMF")

    mock_days.assert_called_once()
    mock_backfill.assert_called_once_with("TM2610", lookback_days=2, interval_minutes_list=(60, 15))


def test_set_product_caps_tmf_lookback_at_5_days_even_long_after_rollover():
    """換月已經過很多天（例如13天)時，lookback_days 封頂在5天，不會無限拉長。"""
    with patch("backend.live_service._svc", return_value=_mock_svc()), \
         patch("backend.live_service.resolve_quote_product_code", return_value="TM2610"), \
         patch("backend.live_service._is_worker_mode", return_value=False), \
         patch("backend.trading_service.days_since_tmf_rollover", return_value=13), \
         patch("backend.shioaji_backfill.backfill_via_shioaji") as mock_backfill:
        from backend.live_service import set_product

        set_product("TMF")

    mock_backfill.assert_called_once_with("TM2610", lookback_days=5, interval_minutes_list=(60, 15))


def test_set_product_uses_fixed_2day_lookback_for_non_tmf_products():
    """大台/小台沒有「距離換月天數」這個安全邊界可以算，維持原本保守的2天，不冒進。"""
    with patch("backend.live_service._svc", return_value=_mock_svc()), \
         patch("backend.live_service.resolve_quote_product_code", return_value="TX"), \
         patch("backend.live_service._is_worker_mode", return_value=False), \
         patch("backend.trading_service.days_since_tmf_rollover") as mock_days, \
         patch("backend.shioaji_backfill.backfill_via_shioaji") as mock_backfill:
        from backend.live_service import set_product

        set_product("TX")

    mock_days.assert_not_called()
    mock_backfill.assert_called_once_with("TX", lookback_days=2, interval_minutes_list=(60, 15))


def test_set_product_skips_shioaji_backfill_in_worker_mode():
    """worker 行程（WORKER_MODE=1）不做群益/Shioaji K線回補，避免多個帳號的
    worker 同時搶著寫同一個商品代碼的K線快取檔案——切換商品時也要遵守同一條規則。"""
    with patch("backend.live_service._svc", return_value=_mock_svc()), \
         patch("backend.live_service.resolve_quote_product_code", return_value="TM2610"), \
         patch("backend.live_service._is_worker_mode", return_value=True), \
         patch("backend.shioaji_backfill.backfill_via_shioaji") as mock_backfill:
        from backend.live_service import set_product

        set_product("TMF")

    mock_backfill.assert_not_called()


def test_set_product_survives_shioaji_backfill_failure_without_crashing():
    """Shioaji 補缺失敗（例如連線例外)不該讓切換商品本身失敗——
    這只是錦上添花的額外補缺，不是切換商品的必要條件。"""
    with patch("backend.live_service._svc", return_value=_mock_svc()), \
         patch("backend.live_service.resolve_quote_product_code", return_value="TM2610"), \
         patch("backend.live_service._is_worker_mode", return_value=False), \
         patch("backend.trading_service.days_since_tmf_rollover", return_value=5), \
         patch("backend.shioaji_backfill.backfill_via_shioaji", side_effect=RuntimeError("boom")):
        from backend.live_service import set_product

        result = set_product("TMF")

    assert result["product_code"] == "TMF"


def test_set_product_updates_last_shioaji_backfill_attempt_timestamp():
    """切換當下已經補過一次，更新共用的節流時間戳，避免幾秒後下一輪定期輪詢
    又立刻重複打一次一模一樣的 Shioaji 查詢。"""
    import backend.live_service as live_service

    live_service._last_shioaji_backfill_attempt = 0.0

    with patch("backend.live_service._svc", return_value=_mock_svc()), \
         patch("backend.live_service.resolve_quote_product_code", return_value="TM2610"), \
         patch("backend.live_service._is_worker_mode", return_value=False), \
         patch("backend.trading_service.days_since_tmf_rollover", return_value=5), \
         patch("backend.shioaji_backfill.backfill_via_shioaji"):
        live_service.set_product("TMF")

    assert live_service._last_shioaji_backfill_attempt > 0.0
