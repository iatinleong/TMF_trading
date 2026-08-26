import time
from unittest.mock import MagicMock, patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from backend.supabase_auth import InvalidSupabaseToken, verify_supabase_jwt


@pytest.fixture()
def keypair():
    private_key = ec.generate_private_key(ec.SECP256R1())
    return private_key, private_key.public_key()


def _make_token(private_key, *, aud="authenticated", exp_delta=3600, iat_delta=0, **extra_claims):
    now = int(time.time())
    payload = {
        "sub": "user-123",
        "email": "trader@example.com",
        "aud": aud,
        "iat": now + iat_delta,
        "exp": now + exp_delta,
        **extra_claims,
    }
    return jwt.encode(payload, private_key, algorithm="ES256")


def _patch_jwk_client(public_key):
    signing_key = MagicMock()
    signing_key.key = public_key
    mock_client = MagicMock()
    mock_client.get_signing_key_from_jwt.return_value = signing_key
    return patch("backend.supabase_auth._get_jwk_client", return_value=mock_client)


def test_verify_supabase_jwt_accepts_valid_token(keypair):
    private_key, public_key = keypair
    token = _make_token(private_key)

    with _patch_jwk_client(public_key):
        claims = verify_supabase_jwt(token)

    assert claims["sub"] == "user-123"
    assert claims["email"] == "trader@example.com"


def test_verify_supabase_jwt_rejects_empty_token():
    with pytest.raises(InvalidSupabaseToken):
        verify_supabase_jwt("")


def test_verify_supabase_jwt_rejects_expired_token(keypair):
    private_key, public_key = keypair
    token = _make_token(private_key, exp_delta=-60)  # 已過期一分鐘

    with _patch_jwk_client(public_key):
        with pytest.raises(InvalidSupabaseToken):
            verify_supabase_jwt(token)


def test_verify_supabase_jwt_rejects_wrong_audience(keypair):
    private_key, public_key = keypair
    token = _make_token(private_key, aud="something-else")

    with _patch_jwk_client(public_key):
        with pytest.raises(InvalidSupabaseToken):
            verify_supabase_jwt(token)


def test_verify_supabase_jwt_tolerates_small_clock_skew(keypair):
    # 2026-08-24 實測抓到的根因：伺服器系統時鐘比實際時間慢，剛簽發的 token
    # iat 對它來說像是「來自未來」，PyJWT 預設會拒絕。leeway=60 應該要能
    # 容忍 30 秒的時鐘飄移，這是驗證這個修復本身有生效的迴歸測試。
    private_key, public_key = keypair
    token = _make_token(private_key, iat_delta=30)

    with _patch_jwk_client(public_key):
        claims = verify_supabase_jwt(token)

    assert claims["sub"] == "user-123"


def test_verify_supabase_jwt_rejects_large_clock_skew(keypair):
    # leeway 是「容忍一點誤差」，不是「乾脆不檢查」——飄移大到不合理（5分鐘）
    # 還是要擋下來。
    private_key, public_key = keypair
    token = _make_token(private_key, iat_delta=300)

    with _patch_jwk_client(public_key):
        with pytest.raises(InvalidSupabaseToken):
            verify_supabase_jwt(token)


def test_verify_supabase_jwt_rejects_token_signed_by_wrong_key(keypair):
    _, public_key = keypair
    other_private_key = ec.generate_private_key(ec.SECP256R1())
    token = _make_token(other_private_key)  # 用另一把私鑰簽的，公鑰對不上

    with _patch_jwk_client(public_key):
        with pytest.raises(InvalidSupabaseToken):
            verify_supabase_jwt(token)
