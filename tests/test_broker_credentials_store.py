"""backend/broker_credentials_store.py 的單元測試：跟 test_strategy_config_store.py
用同一套 mock requests 的模式，不打真的 Supabase。這張表存真實交易密碼，
測試裡一律用假字串，絕對不要用真實帳密（就算是測試帳號也不要）。
"""

from unittest.mock import MagicMock, patch

import pytest

from backend.broker_credentials_store import (
    admin_set_broker_credentials,
    get_broker_credentials,
)


def test_get_returns_none_when_not_configured(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)

    assert get_broker_credentials("user-123") is None


def test_get_returns_none_when_no_row_exists(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")

    fake_response = MagicMock()
    fake_response.json.return_value = []
    fake_response.raise_for_status.return_value = None

    with patch("backend.broker_credentials_store.requests.get", return_value=fake_response):
        assert get_broker_credentials("user-123") is None


def test_get_returns_credentials_when_row_exists(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")

    fake_response = MagicMock()
    fake_response.json.return_value = [
        {"user_id": "user-123", "capital_user_id": "A123456789", "capital_password": "fake_pw"}
    ]
    fake_response.raise_for_status.return_value = None

    with patch("backend.broker_credentials_store.requests.get", return_value=fake_response) as mock_get:
        creds = get_broker_credentials("user-123")

    assert creds == {"capital_user_id": "A123456789", "capital_password": "fake_pw"}
    assert mock_get.call_args.kwargs["params"]["user_id"] == "eq.user-123"


def test_get_returns_none_on_request_failure(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")

    with patch("backend.broker_credentials_store.requests.get", side_effect=RuntimeError("網路錯誤")):
        assert get_broker_credentials("user-123") is None


def test_admin_set_raises_when_not_configured(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)

    with pytest.raises(RuntimeError):
        admin_set_broker_credentials(
            "user-123", capital_user_id="A123456789", capital_password="fake_pw",
        )


def test_admin_set_posts_correct_payload(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")

    fake_response = MagicMock()
    fake_response.json.return_value = [
        {"user_id": "user-123", "capital_user_id": "A123456789", "capital_password": "fake_pw"}
    ]
    fake_response.raise_for_status.return_value = None

    with patch("backend.broker_credentials_store.requests.post", return_value=fake_response) as mock_post:
        row = admin_set_broker_credentials(
            "user-123", capital_user_id="A123456789", capital_password="fake_pw",
        )

    assert row["capital_user_id"] == "A123456789"
    called_url = mock_post.call_args.args[0]
    assert called_url == "https://example.supabase.co/rest/v1/user_broker_credentials"
    payload = mock_post.call_args.kwargs["json"]
    assert payload == {
        "user_id": "user-123",
        "capital_user_id": "A123456789",
        "capital_password": "fake_pw",
    }
    assert mock_post.call_args.kwargs["params"]["on_conflict"] == "user_id"
