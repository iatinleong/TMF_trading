"""backend/live_service.py 的 worker 模式支援測試：ACCOUNT_USER_ID 設定時，
應該用該帳號自己的群益帳密覆蓋掉共用的 CAPITAL_USER_ID/CAPITAL_PASSWORD；
WORKER_MODE 設定時，poll_once() 的 K 線擁有邏輯（試讀群益歷史 K 線、
Shioaji 定期補缺）應該被跳過——K 線只由沒有設定 WORKER_MODE 的那個共用
行程負責讀寫。
"""

import os
from unittest.mock import patch

from backend.live_service import _apply_worker_account_credentials, _is_worker_mode


def test_is_worker_mode_false_by_default(monkeypatch):
    monkeypatch.delenv("WORKER_MODE", raising=False)
    assert _is_worker_mode() is False


def test_is_worker_mode_true_when_set(monkeypatch):
    monkeypatch.setenv("WORKER_MODE", "1")
    assert _is_worker_mode() is True


def test_apply_worker_credentials_noop_without_account_user_id(monkeypatch):
    monkeypatch.delenv("ACCOUNT_USER_ID", raising=False)
    monkeypatch.setenv("CAPITAL_USER_ID", "original_user")

    _apply_worker_account_credentials()

    assert os.environ["CAPITAL_USER_ID"] == "original_user"


def test_apply_worker_credentials_overrides_when_account_user_id_set(monkeypatch):
    monkeypatch.setenv("ACCOUNT_USER_ID", "supabase-user-123")
    monkeypatch.setenv("CAPITAL_USER_ID", "shared_default_user")

    with patch(
        "backend.live_service.get_broker_credentials",
        return_value={"capital_user_id": "A_from_worker_account", "capital_password": "fake_pw"},
    ) as mock_get:
        _apply_worker_account_credentials()

    mock_get.assert_called_once_with("supabase-user-123")
    assert os.environ["CAPITAL_USER_ID"] == "A_from_worker_account"
    assert os.environ["CAPITAL_PASSWORD"] == "fake_pw"


def test_apply_worker_credentials_leaves_env_unchanged_when_account_not_found(monkeypatch):
    monkeypatch.setenv("ACCOUNT_USER_ID", "supabase-user-123")
    monkeypatch.setenv("CAPITAL_USER_ID", "original_user")

    with patch("backend.live_service.get_broker_credentials", return_value=None):
        _apply_worker_account_credentials()

    assert os.environ["CAPITAL_USER_ID"] == "original_user"


def test_load_project_env_preserves_worker_credentials_after_remote_secrets(monkeypatch):
    """驗證 _load_project_env 載入遠端共用密鑰後，會自動保證 Worker 自己的帳密不被翻盤。"""
    from backend.live_service import _load_project_env

    monkeypatch.setenv("ACCOUNT_USER_ID", "user-b-uuid")
    monkeypatch.setenv("CAPITAL_USER_ID", "initial_user")

    # 模擬遠端 app_secrets 寫入共用主帳號
    def mock_load_remote():
        os.environ["CAPITAL_USER_ID"] = "SHARED_MAIN_ACCOUNT"
        os.environ["CAPITAL_PASSWORD"] = "SHARED_PASSWORD"
        return 2

    with patch("backend.live_service.load_remote_secrets", side_effect=mock_load_remote), \
         patch(
             "backend.live_service.get_broker_credentials",
             return_value={"capital_user_id": "WORKER_B_ACCOUNT", "capital_password": "WORKER_B_PASSWORD"},
         ):
        _load_project_env()

    assert os.environ["CAPITAL_USER_ID"] == "WORKER_B_ACCOUNT"
    assert os.environ["CAPITAL_PASSWORD"] == "WORKER_B_PASSWORD"


def test_auto_connect_on_startup_preserves_worker_credentials_during_connect(monkeypatch):
    """驗證 auto_connect_on_startup 執行連線時，絕不會被 _default_environment 覆蓋回共用主帳號。"""
    from backend.live_service import auto_connect_on_startup

    monkeypatch.setenv("WORKER_MODE", "1")
    monkeypatch.setenv("ACCOUNT_USER_ID", "user-b-uuid")
    monkeypatch.setenv("AUTO_CONNECT", "1")
    monkeypatch.setattr("platform.system", lambda: "Windows")

    def mock_load_remote():
        os.environ["CAPITAL_USER_ID"] = "SHARED_MAIN_ACCOUNT"
        os.environ["CAPITAL_PASSWORD"] = "SHARED_PASSWORD"
        return 2

    with patch("backend.live_service.load_remote_secrets", side_effect=mock_load_remote), \
         patch(
             "backend.live_service.get_broker_credentials",
             return_value={"capital_user_id": "WORKER_B_ACCOUNT", "capital_password": "WORKER_B_PASSWORD"},
         ), \
         patch("backend.trading_service.TradingService.connect") as mock_connect:
        auto_connect_on_startup()

    assert os.environ["CAPITAL_USER_ID"] == "WORKER_B_ACCOUNT"
    assert os.environ["CAPITAL_PASSWORD"] == "WORKER_B_PASSWORD"
    mock_connect.assert_called_once()
