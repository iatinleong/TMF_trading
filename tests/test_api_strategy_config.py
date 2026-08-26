"""backend/api.py 新增的帳號↔策略設定端點測試。跟 test_api_auth.py 一樣，不用
TestClient 啟動整個 app（lifespan 會嘗試連線真實群益 COM），直接呼叫 route
handler function，用假的 Request 帶 request.state.user_id/user_email。
"""

from unittest.mock import patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from backend.api import (
    AdminStrategyConfigRequest,
    ToggleStrategyRequest,
    api_admin_strategy_defs,
    api_admin_upsert_strategy_config,
    api_me,
    api_my_strategy_configs,
    api_toggle_my_strategy_config,
)


def _make_request(*, user_id: str = "", user_email: str = "") -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/me",
        "query_string": b"",
        "headers": [],
        "client": ("test", 0),
    }
    request = Request(scope)
    request.state.user_id = user_id
    request.state.user_email = user_email
    return request


def test_api_me_returns_identity_and_admin_flag(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    request = _make_request(user_id="user-123", user_email="boss@example.com")

    result = api_me(request)

    assert result == {"user_id": "user-123", "email": "boss@example.com", "is_admin": True}


def test_api_me_non_admin_user(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    request = _make_request(user_id="user-456", user_email="someone-else@example.com")

    result = api_me(request)

    assert result == {"user_id": "user-456", "email": "someone-else@example.com", "is_admin": False}


def test_admin_strategy_defs_rejects_non_admin(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    request = _make_request(user_id="user-456", user_email="someone-else@example.com")

    with pytest.raises(HTTPException) as exc_info:
        api_admin_strategy_defs(request)
    assert exc_info.value.status_code == 403


def test_admin_strategy_defs_lists_all_four_for_admin(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    request = _make_request(user_id="admin-1", user_email="boss@example.com")

    catalog = api_admin_strategy_defs(request)

    ids = {row["strategy_id"] for row in catalog}
    assert ids == {"breakout_long", "breakout_short", "pullback_long", "pullback_short"}


def test_admin_upsert_rejects_non_admin(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    request = _make_request(user_id="user-456", user_email="someone-else@example.com")
    body = AdminStrategyConfigRequest(user_id="user-789", strategy_id="breakout_long")

    with pytest.raises(HTTPException) as exc_info:
        api_admin_upsert_strategy_config(request, body)
    assert exc_info.value.status_code == 403


def test_admin_upsert_rejects_unknown_strategy_id(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    request = _make_request(user_id="admin-1", user_email="boss@example.com")
    body = AdminStrategyConfigRequest(user_id="user-789", strategy_id="not_real")

    with pytest.raises(HTTPException) as exc_info:
        api_admin_upsert_strategy_config(request, body)
    assert exc_info.value.status_code == 400


def test_admin_upsert_calls_store_for_target_user(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    request = _make_request(user_id="admin-1", user_email="boss@example.com")
    body = AdminStrategyConfigRequest(
        user_id="user-789", strategy_id="breakout_long", product_code="TM2608", qty=3, enabled=False,
    )

    with patch(
        "backend.api.admin_upsert_strategy_config",
        return_value={"strategy_id": "breakout_long", "user_id": "user-789"},
    ) as mock_upsert:
        result = api_admin_upsert_strategy_config(request, body)

    mock_upsert.assert_called_once_with(
        "user-789", "breakout_long", product_code="TM2608", qty=3, enabled=False,
    )
    assert result == {"strategy_id": "breakout_long", "user_id": "user-789"}


def test_my_strategy_configs_requires_login():
    request = _make_request(user_id="")

    with pytest.raises(HTTPException) as exc_info:
        api_my_strategy_configs(request)
    assert exc_info.value.status_code == 401


def test_my_strategy_configs_returns_only_this_users_rows():
    request = _make_request(user_id="user-123")

    with patch(
        "backend.api.list_user_strategy_configs",
        return_value=[{"strategy_id": "breakout_long", "enabled": True}],
    ) as mock_list:
        result = api_my_strategy_configs(request)

    mock_list.assert_called_once_with("user-123")
    assert result == [{"strategy_id": "breakout_long", "enabled": True}]


def test_my_strategy_configs_empty_for_account_with_no_custom_strategy():
    # 新帳號、還沒被管理員客製化過任何策略——回傳空 list，不是錯誤。
    request = _make_request(user_id="brand-new-user")

    with patch("backend.api.list_user_strategy_configs", return_value=[]):
        result = api_my_strategy_configs(request)

    assert result == []


def test_toggle_requires_login():
    request = _make_request(user_id="")
    body = ToggleStrategyRequest(strategy_id="breakout_long", enabled=True)

    with pytest.raises(HTTPException) as exc_info:
        api_toggle_my_strategy_config(request, body)
    assert exc_info.value.status_code == 401


def test_toggle_returns_404_when_not_customized_for_this_user():
    request = _make_request(user_id="user-123")
    body = ToggleStrategyRequest(strategy_id="breakout_long", enabled=True)

    with patch("backend.api.set_strategy_enabled", return_value=None) as mock_set:
        with pytest.raises(HTTPException) as exc_info:
            api_toggle_my_strategy_config(request, body)

    mock_set.assert_called_once_with("user-123", "breakout_long", True)
    assert exc_info.value.status_code == 404


def test_toggle_updates_existing_row():
    request = _make_request(user_id="user-123")
    body = ToggleStrategyRequest(strategy_id="breakout_long", enabled=False)

    with patch(
        "backend.api.set_strategy_enabled",
        return_value={"strategy_id": "breakout_long", "enabled": False},
    ) as mock_set:
        result = api_toggle_my_strategy_config(request, body)

    mock_set.assert_called_once_with("user-123", "breakout_long", False)
    assert result == {"strategy_id": "breakout_long", "enabled": False}
