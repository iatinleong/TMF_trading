"""backend/strategy_config_store.py 的單元測試：跟 test_secrets_store.py 用
同一套 mock requests 的模式，不打真的 Supabase。
"""

from unittest.mock import MagicMock, patch

import pytest

from backend.strategy_config_store import (
    admin_upsert_strategy_config,
    list_user_strategy_configs,
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


def test_admin_upsert_posts_risk_parameters(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")

    fake_response = MagicMock()
    fake_response.json.return_value = [
        {"strategy_id": "breakout_long", "user_id": "user-456", "enabled": True}
    ]
    fake_response.raise_for_status.return_value = None

    with patch("backend.strategy_config_store.requests.post", return_value=fake_response) as mock_post:
        row = admin_upsert_strategy_config(
            "user-456",
            "breakout_long",
            product_code="TM2609",
            qty=1,
            enabled=True,
            stop_loss_points=80.0,
            take_profit_points=200.0,
            max_loss_ntd=5000.0,
            max_loss_pct=0.05,
        )

    payload = mock_post.call_args.kwargs["json"]
    assert payload == {
        "user_id": "user-456",
        "strategy_id": "breakout_long",
        "product_code": "TM2609",
        "qty": 1,
        "enabled": True,
        "stop_loss_points": 80.0,
        "take_profit_points": 200.0,
        "max_loss_ntd": 5000.0,
        "max_loss_pct": 0.05,
    }
