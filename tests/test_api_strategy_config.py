"""backend/api.py 新增的帳號↔策略設定端點測試。跟 test_api_auth.py 一樣，不用
TestClient 啟動整個 app（lifespan 會嘗試連線真實群益 COM），直接呼叫 route
handler function，用假的 Request 帶 request.state.user_id/user_email。
"""

from unittest.mock import patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from backend.api import api_me


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
