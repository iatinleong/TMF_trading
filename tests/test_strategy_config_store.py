"""backend/strategy_config_store.py 的單元測試：跟 test_secrets_store.py 用
同一套 mock requests 的模式，不打真的 Supabase。
"""

from unittest.mock import MagicMock, patch

import pytest

from backend.strategy_config_store import (
    admin_upsert_strategy_config,
    list_user_strategy_configs,
    set_strategy_enabled,
)


def test_list_returns_empty_when_not_configured(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)

    assert list_user_strategy_configs("user-123") == []


def test_list_returns_rows_on_success(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")

    fake_response = MagicMock()
    fake_response.json.return_value = [{"strategy_id": "breakout_long", "enabled": True}]
    fake_response.raise_for_status.return_value = None

    with patch("backend.strategy_config_store.requests.get", return_value=fake_response) as mock_get:
        rows = list_user_strategy_configs("user-123")

    assert rows == [{"strategy_id": "breakout_long", "enabled": True}]
    called_url = mock_get.call_args.args[0]
    assert called_url == "https://example.supabase.co/rest/v1/user_strategy_configs"
    assert mock_get.call_args.kwargs["params"]["user_id"] == "eq.user-123"


def test_list_returns_empty_on_request_failure(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")

    with patch("backend.strategy_config_store.requests.get", side_effect=RuntimeError("網路錯誤")):
        assert list_user_strategy_configs("user-123") == []


def test_set_enabled_returns_none_when_row_does_not_exist(monkeypatch):
    # 這個使用者根本沒被客製化過這個策略——PostgREST 對「沒有任何列符合篩選
    # 條件」的 PATCH 回傳 200 + 空陣列，不是錯誤，所以用回傳值分辨，不是例外。
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")

    fake_response = MagicMock()
    fake_response.json.return_value = []
    fake_response.raise_for_status.return_value = None

    with patch("backend.strategy_config_store.requests.patch", return_value=fake_response):
        result = set_strategy_enabled("user-123", "breakout_long", True)

    assert result is None


def test_set_enabled_updates_existing_row(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")

    fake_response = MagicMock()
    fake_response.json.return_value = [
        {"user_id": "user-123", "strategy_id": "breakout_long", "enabled": False}
    ]
    fake_response.raise_for_status.return_value = None

    with patch("backend.strategy_config_store.requests.patch", return_value=fake_response) as mock_patch:
        result = set_strategy_enabled("user-123", "breakout_long", False)

    assert result == {"user_id": "user-123", "strategy_id": "breakout_long", "enabled": False}
    assert mock_patch.call_args.kwargs["params"]["user_id"] == "eq.user-123"
    assert mock_patch.call_args.kwargs["params"]["strategy_id"] == "eq.breakout_long"
    assert mock_patch.call_args.kwargs["json"] == {"enabled": False}


def test_admin_upsert_raises_when_not_configured(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)

    with pytest.raises(RuntimeError):
        admin_upsert_strategy_config(
            "user-123", "breakout_long", product_code="TM2608", qty=1, enabled=False,
        )


def test_admin_upsert_posts_correct_payload_and_returns_row(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")

    fake_response = MagicMock()
    fake_response.json.return_value = [
        {"strategy_id": "breakout_long", "user_id": "user-456", "enabled": False, "qty": 2}
    ]
    fake_response.raise_for_status.return_value = None

    with patch("backend.strategy_config_store.requests.post", return_value=fake_response) as mock_post:
        row = admin_upsert_strategy_config(
            "user-456", "breakout_long", product_code="TM2608", qty=2, enabled=False,
        )

    assert row["qty"] == 2
    called_url = mock_post.call_args.args[0]
    assert called_url == "https://example.supabase.co/rest/v1/user_strategy_configs"
    payload = mock_post.call_args.kwargs["json"]
    assert payload == {
        "user_id": "user-456",
        "strategy_id": "breakout_long",
        "product_code": "TM2608",
        "qty": 2,
        "enabled": False,
    }
    assert mock_post.call_args.kwargs["params"]["on_conflict"] == "user_id,strategy_id"
