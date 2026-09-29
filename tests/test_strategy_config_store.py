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


def test_admin_upsert_posts_layer_toggles(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")

    fake_response = MagicMock()
    fake_response.json.return_value = [
        {"strategy_id": "breakout_long", "user_id": "user-456", "enabled": True}
    ]
    fake_response.raise_for_status.return_value = None

    with patch("backend.strategy_config_store.requests.post", return_value=fake_response) as mock_post:
        admin_upsert_strategy_config(
            "user-456",
            "breakout_long",
            product_code="TM2609",
            qty=1,
            enabled=True,
            oco_enabled=False,
            soft_stop_enabled=True,
            risk_insurance_enabled=False,
            reverse_signal_exit_enabled=True,
        )

    payload = mock_post.call_args.kwargs["json"]
    assert payload == {
        "user_id": "user-456",
        "strategy_id": "breakout_long",
        "product_code": "TM2609",
        "qty": 1,
        "enabled": True,
        "oco_enabled": False,
        "soft_stop_enabled": True,
        "risk_insurance_enabled": False,
        "reverse_signal_exit_enabled": True,
    }


def test_admin_upsert_omits_layer_toggles_when_not_specified(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")

    fake_response = MagicMock()
    fake_response.json.return_value = [{"strategy_id": "breakout_long"}]
    fake_response.raise_for_status.return_value = None

    with patch("backend.strategy_config_store.requests.post", return_value=fake_response) as mock_post:
        admin_upsert_strategy_config(
            "user-456", "breakout_long", product_code="TM2609", qty=1, enabled=True,
        )

    payload = mock_post.call_args.kwargs["json"]
    assert "oco_enabled" not in payload
    assert "soft_stop_enabled" not in payload
    assert "risk_insurance_enabled" not in payload
    assert "reverse_signal_exit_enabled" not in payload


def test_admin_upsert_omits_qty_when_not_specified(monkeypatch):
    """qty 是 nullable 欄位（沒有 NOT NULL 限制)，省略時安全地不送進 payload，
    讓呼叫端可以「只更新其他欄位、不動 qty」而不會意外把既有值蓋成 null。"""
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")

    fake_response = MagicMock()
    fake_response.json.return_value = [{"strategy_id": "death_cross_short"}]
    fake_response.raise_for_status.return_value = None

    with patch("backend.strategy_config_store.requests.post", return_value=fake_response) as mock_post:
        admin_upsert_strategy_config("user-456", "death_cross_short", product_code="TMF", enabled=False)

    payload = mock_post.call_args.kwargs["json"]
    assert payload["product_code"] == "TMF"
    assert "qty" not in payload


def test_admin_upsert_raises_when_product_code_missing(monkeypatch):
    """2026-09-30 最終審查抓到的問題：product_code 不能比照 qty 那樣「省略就安全」
    ——user_strategy_configs.product_code 的資料庫欄位預設值目前已知損壞（實測讀回
    來是字面字串 "'TMF'::text"，不是乾淨的 "TMF"),一旦真的被拿去下單，
    resolve_strategy_product_code() 認不得這個格式，可能導致 _contract_from_product()
    誤判成大台（point_value 200，是微台 10 的 20 倍),跟這個檔案上方註解記錄過的
    2026-08-21 實盤事故是同一種風險。在資料庫欄位預設值修好之前，product_code
    必須是呼叫端明確提供的必填參數，不能靠省略退回資料庫預設值。"""
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")

    with patch("backend.strategy_config_store.requests.post") as mock_post:
        with pytest.raises(ValueError, match="product_code"):
            admin_upsert_strategy_config("user-456", "death_cross_short", enabled=False)

    mock_post.assert_not_called()


def test_admin_upsert_omits_enabled_when_not_specified(monkeypatch):
    """同一種 bug、同一個共用函式：user_strategy_configs.enabled 也是
    NOT NULL DEFAULT false。scripts/bind_strategy.py 的 --enabled/--disabled
    是共用 dest="enabled"、預設值 None 的一對旗標——這次任務實際執行
    `bind_strategy.py --user-id ... --strategy-id death_cross_short
    --no-reverse-signal-exit-enabled`（沒帶 --enabled/--disabled）時，
    args.enabled 是 None，明確傳進 admin_upsert_strategy_config(enabled=None)
    一樣會送出 null、被 PostgREST 回 400，是修 product_code/qty 那次沒一併
    處理到的同一種問題。"""
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")

    fake_response = MagicMock()
    fake_response.json.return_value = [{"strategy_id": "death_cross_short"}]
    fake_response.raise_for_status.return_value = None

    with patch("backend.strategy_config_store.requests.post", return_value=fake_response) as mock_post:
        admin_upsert_strategy_config("user-456", "death_cross_short", product_code="TMF", enabled=None)

    payload = mock_post.call_args.kwargs["json"]
    assert "enabled" not in payload
