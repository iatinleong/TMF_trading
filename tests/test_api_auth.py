"""
只測 require_supabase_auth middleware 本身，不透過 TestClient 啟動整個 app——
api.py 的 lifespan 會嘗試連線群益 COM（僅 Windows 硬體環境可用、且是真實連線），
測試環境不該觸發到那一段。middleware 是一般 async function，直接呼叫最乾淨。
"""

from unittest.mock import AsyncMock, patch

import pytest
from starlette.requests import Request

from backend.api import require_supabase_auth
from backend.supabase_auth import InvalidSupabaseToken


def _make_request(path: str, *, query_string: bytes = b"", headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": query_string,
        "headers": headers or [],
        "client": ("test", 0),
    }
    return Request(scope)


@pytest.mark.anyio
async def test_health_endpoint_bypasses_auth():
    request = _make_request("/api/health")
    call_next = AsyncMock(return_value="ok-response")

    result = await require_supabase_auth(request, call_next)

    call_next.assert_called_once()
    assert result == "ok-response"


@pytest.mark.anyio
async def test_static_frontend_paths_bypass_auth():
    # 登入頁本身也是靜態檔案，不能被 middleware 擋住，不然登入頁自己都載不出來。
    request = _make_request("/login.html")
    call_next = AsyncMock(return_value="ok-response")

    result = await require_supabase_auth(request, call_next)

    call_next.assert_called_once()
    assert result == "ok-response"


@pytest.mark.anyio
async def test_api_endpoint_without_token_returns_401():
    request = _make_request("/api/positions")
    call_next = AsyncMock(return_value="should-not-be-called")

    with patch("backend.api.verify_supabase_jwt", side_effect=InvalidSupabaseToken("缺少 token")):
        response = await require_supabase_auth(request, call_next)

    call_next.assert_not_called()
    assert response.status_code == 401


@pytest.mark.anyio
async def test_api_endpoint_with_valid_bearer_token_passes_through():
    request = _make_request(
        "/api/positions",
        headers=[(b"authorization", b"Bearer valid-token-here")],
    )
    call_next = AsyncMock(return_value="ok-response")

    with patch("backend.api.verify_supabase_jwt", return_value={"sub": "user-123"}) as mock_verify:
        result = await require_supabase_auth(request, call_next)

    mock_verify.assert_called_once_with("valid-token-here")
    call_next.assert_called_once()
    assert result == "ok-response"


@pytest.mark.anyio
async def test_api_endpoint_with_invalid_token_returns_401():
    request = _make_request(
        "/api/positions",
        headers=[(b"authorization", b"Bearer garbage")],
    )
    call_next = AsyncMock(return_value="should-not-be-called")

    with patch("backend.api.verify_supabase_jwt", side_effect=InvalidSupabaseToken("bad sig")):
        response = await require_supabase_auth(request, call_next)

    call_next.assert_not_called()
    assert response.status_code == 401


@pytest.mark.anyio
async def test_api_endpoint_accepts_token_via_query_param():
    # WebSocket 沿用同一個驗證函式，但瀏覽器 WS 不能帶自訂 header，只能靠
    # query string——這裡順便確認一般 HTTP middleware 也認 ?token=。
    request = _make_request("/api/positions", query_string=b"token=from-query")
    call_next = AsyncMock(return_value="ok-response")

    with patch("backend.api.verify_supabase_jwt", return_value={"sub": "user-123"}) as mock_verify:
        result = await require_supabase_auth(request, call_next)

    mock_verify.assert_called_once_with("from-query")
    assert result == "ok-response"
