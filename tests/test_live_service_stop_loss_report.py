"""backend/live_service.py::query_stop_loss_report() 的測試。

2026-10-16 code review 抓到的真實漏洞：TradingService.query_stop_loss_report()
的 kind 預設值曾經一度被改成 "OCO"（誤以為這個函式只有長效單序號查詢在用），
但 /api/order/stop/report 這個既有端點（這裡測的 query_stop_loss_report）一直
都不帶 kind、吃那邊的預設值——改掉等於讓這個名字叫「stop/report」的端點悄悄
開始查 OCO 資料，對任何還在查 STP 單的呼叫端回傳錯誤/空白結果。這裡直接鎖住
「這個函式會明確傳 kind='STP'」，不要依賴對方的預設值，兩邊都有防呆。
"""

from unittest.mock import MagicMock, patch

from backend.live_service import query_stop_loss_report


def test_query_stop_loss_report_explicitly_requests_stp_kind():
    mock_svc = MagicMock()
    mock_svc.query_stop_loss_report.return_value = {"stop_loss_raw": "", "stop_loss_rows": []}

    with patch("backend.live_service._svc", return_value=mock_svc):
        query_stop_loss_report()

    mock_svc.query_stop_loss_report.assert_called_once_with(None, kind="STP")
