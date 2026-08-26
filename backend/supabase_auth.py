"""
backend/supabase_auth.py — 驗證前端登入用的 Supabase JWT（2026-08-21 取代原本的
ACCESS_KEY 共用金鑰驗證，見 api.py 的 require_supabase_auth middleware）。

這個專案的 Supabase 專案用的是非對稱簽章（ES256，實際查過
https://<project>.supabase.co/auth/v1/.well-known/jwks.json 確認過，不是猜的）。
驗證 token 只需要公開金鑰（JWKS），後端完全不用存放任何 Supabase 密鑰，純粹拿
公開金鑰驗簽章＋檢查效期/aud，不需要每次 API 呼叫都連回 Supabase（低延遲，
交易看板不能每個請求都多一趟外部網路）。
"""

from __future__ import annotations

import logging
import os
from typing import Any

import jwt
from jwt import PyJWKClient

logger = logging.getLogger(__name__)


class InvalidSupabaseToken(Exception):
    """token 缺失、格式錯誤、簽章驗證失敗、或已過期。"""


_jwk_client: PyJWKClient | None = None


def _jwks_url() -> str:
    supabase_url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    if not supabase_url:
        raise RuntimeError("SUPABASE_URL 未設定，無法驗證登入")
    return f"{supabase_url}/auth/v1/.well-known/jwks.json"


def _get_jwk_client() -> PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        # PyJWKClient 內建快取＋自動處理 key rotation（kid 對不上時會重抓一次）。
        _jwk_client = PyJWKClient(_jwks_url(), cache_keys=True, lifespan=3600)
    return _jwk_client


def reset_jwk_client_cache() -> None:
    """測試/SUPABASE_URL 變更後用，強制下次驗證重新建立 JWKS client。"""
    global _jwk_client
    _jwk_client = None


def verify_supabase_jwt(token: str) -> dict[str, Any]:
    """
    驗證 Supabase 登入發出的 access token，回傳解出來的 claims（含 sub/email/exp）。
    驗證失敗一律拋 InvalidSupabaseToken，呼叫端不用分辨是哪種失敗原因，
    只要「不是有效登入」就一律擋下來。
    """
    if not token:
        raise InvalidSupabaseToken("缺少 token")
    try:
        signing_key = _get_jwk_client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["ES256"],
            audience="authenticated",
            # 2026-08-24 實測抓到的真正根因：這台機器的系統時鐘比實際時間慢，
            # 剛簽發的 token 的 iat（簽發時間）對這台機器來說像是「來自未來」，
            # 被 PyJWT 拒絕（"The token is not yet valid (iat)"），要等系統時鐘
            # 追上才會開始接受——log 裡一長串 401 後忽然變 200 就是這個現象。
            # 加時間容忍度是業界標準做法（分散式系統的時鐘本來就不會完全同步，
            # 不是只有這台機器需要），不是繞過安全檢查，60 秒對 token 有效期
            # 動輒以小時計算的登入場景完全不構成風險。
            leeway=60,
        )
    except InvalidSupabaseToken:
        raise
    except Exception as exc:  # noqa: BLE001
        # 2026-08-21：middleware 對外一律只回「請先登入」，實際失敗原因（簽章
        # 不合、JWKS 抓不到、aud 不符…）需要留在 log 才查得出來，不然一出問題
        # 完全沒有線索可以往下查。
        logger.warning("Supabase JWT 驗證失敗: %s", exc)
        raise InvalidSupabaseToken(str(exc)) from exc
    return claims
