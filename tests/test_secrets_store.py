"""backend/secrets_store.py 的單元測試：確認遠端密鑰讀取失敗/沒設定時安靜
退回、成功時正確覆蓋 os.environ，且不會意外用空字串蓋掉既有值。"""

import os
from unittest.mock import MagicMock, patch

from backend.secrets_store import load_remote_secrets


def test_load_remote_secrets_skips_when_not_configured(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)

    with patch("backend.secrets_store.requests.get") as mock_get:
        applied = load_remote_secrets()

    assert applied == 0
    mock_get.assert_not_called()


def test_load_remote_secrets_applies_values_on_success(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "test-service-role-key")
    monkeypatch.delenv("CAPITAL_PASSWORD", raising=False)

    mock_response = MagicMock()
    mock_response.json.return_value = [
        {
            "CAPITAL_USER_ID": "remote_user",
            "CAPITAL_PASSWORD": "remote_pass",
            "SJ_API_KEY": "remote_sj_key",
            "SJ_SEC_KEY": "remote_sj_secret",
            "SJ_PRODUCTION": "true",
        }
    ]
    mock_response.raise_for_status.return_value = None

    with patch("backend.secrets_store.requests.get", return_value=mock_response) as mock_get:
        applied = load_remote_secrets()

    assert applied == 5
    assert os.environ["CAPITAL_PASSWORD"] == "remote_pass"
    assert os.environ["SJ_API_KEY"] == "remote_sj_key"
    call_kwargs = mock_get.call_args.kwargs
    assert call_kwargs["headers"]["apikey"] == "test-service-role-key"


def test_load_remote_secrets_returns_zero_on_request_failure(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "test-service-role-key")

    with patch("backend.secrets_store.requests.get", side_effect=RuntimeError("network down")):
        applied = load_remote_secrets()

    assert applied == 0


def test_load_remote_secrets_ignores_empty_row_values(monkeypatch):
    """遠端表裡某個欄位是空字串/None 時不該用空值蓋掉本機既有的值。"""
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "test-service-role-key")
    monkeypatch.setenv("SJ_PRODUCTION", "local_fallback_value")

    mock_response = MagicMock()
    mock_response.json.return_value = [{"SJ_PRODUCTION": "", "SJ_API_KEY": None}]
    mock_response.raise_for_status.return_value = None

    with patch("backend.secrets_store.requests.get", return_value=mock_response):
        applied = load_remote_secrets()

    assert applied == 0
    assert os.environ["SJ_PRODUCTION"] == "local_fallback_value"
