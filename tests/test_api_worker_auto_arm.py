"""worker 行程啟動時，應該把 ACCOUNT_USER_ID 這個帳號已經綁定且 enabled=true
的策略自動 arm 起來——這是 worker 專屬行為，不是使用者在網頁上按開關（那個
機制已經在 2026-08-26 的帳號↔策略配對計畫裡拿掉了，見那份計畫文件）。
"""

from unittest.mock import patch

from backend.api import _auto_arm_bound_strategies


def test_does_nothing_when_not_worker_mode(monkeypatch):
    monkeypatch.delenv("WORKER_MODE", raising=False)

    with patch("backend.api.start_strategy") as mock_start:
        _auto_arm_bound_strategies()

    mock_start.assert_not_called()


def test_does_nothing_when_account_user_id_unset(monkeypatch):
    monkeypatch.setenv("WORKER_MODE", "1")
    monkeypatch.delenv("ACCOUNT_USER_ID", raising=False)

    with patch("backend.api.start_strategy") as mock_start:
        _auto_arm_bound_strategies()

    mock_start.assert_not_called()


def test_arms_only_enabled_bound_strategies(monkeypatch):
    monkeypatch.setenv("WORKER_MODE", "1")
    monkeypatch.setenv("ACCOUNT_USER_ID", "user-123")

    rows = [
        {
            "strategy_id": "breakout_long",
            "product_code": "TM2609",
            "qty": 2,
            "enabled": True,
            "stop_loss_points": 80.0,
            "take_profit_points": 200.0,
            "max_loss_ntd": 5000.0,
            "max_loss_pct": 0.05,
            "oco_enabled": False,
            "soft_stop_enabled": True,
            "risk_insurance_enabled": True,
            "reverse_signal_exit_enabled": False,
        },
        {"strategy_id": "breakout_short", "product_code": "TM2609", "qty": 1, "enabled": False},
    ]
    with patch("backend.api.list_user_strategy_configs", return_value=rows) as mock_list, \
         patch("backend.api.start_strategy") as mock_start:
        _auto_arm_bound_strategies()

    mock_list.assert_called_once_with("user-123")
    mock_start.assert_called_once_with(
        "breakout_long",
        "TM2609",
        qty=2,
        stop_loss_points=80.0,
        take_profit_points=200.0,
        max_loss_ntd=5000.0,
        max_loss_pct=0.05,
        oco_enabled=False,
        soft_stop_enabled=True,
        risk_insurance_enabled=True,
        reverse_signal_exit_enabled=False,
    )
