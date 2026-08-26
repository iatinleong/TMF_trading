"""backend/api.py 新增的帳號↔策略設定端點測試。跟 test_api_auth.py 一樣，不用
TestClient 啟動整個 app（lifespan 會嘗試連線真實群益 COM），直接呼叫 route
handler function，用假的 Request 帶 request.state.user_id/user_email。

綁定策略給帳號是工程操作（見 scripts/bind_strategy.py），不透過 Dashboard
網頁，所以這裡沒有管理員端點/開關端點的測試——那些已經拿掉了。
"""

from unittest.mock import patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from backend.api import api_me, api_my_strategy_configs


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


def test_api_me_returns_identity():
    request = _make_request(user_id="user-123", user_email="a@example.com")

    result = api_me(request)

    assert result == {"user_id": "user-123", "email": "a@example.com"}


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
    # 新帳號、還沒被綁定過任何策略——回傳空 list，不是錯誤。
    request = _make_request(user_id="brand-new-user")

    with patch("backend.api.list_user_strategy_configs", return_value=[]):
        result = api_my_strategy_configs(request)

    assert result == []
