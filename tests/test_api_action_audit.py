from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient

from backend.api import app, _action_audit_log_path, append_action_audit


def test_action_audit_log_path_isolation(monkeypatch, tmp_path):
    monkeypatch.setattr("backend.api.DATA_DIR", tmp_path)
    monkeypatch.delenv("ACCOUNT_USER_ID", raising=False)

    default_path = _action_audit_log_path()
    assert default_path == tmp_path / "action_audit.log"

    user_path = _action_audit_log_path("user-uuid-123")
    assert user_path == tmp_path / "users" / "user-uuid-123" / "action_audit.log"


def test_append_action_audit_writes_record(monkeypatch, tmp_path):
    monkeypatch.setattr("backend.api.DATA_DIR", tmp_path)

    append_action_audit(
        action="TEST_ACTION",
        user_id="user-123",
        user_email="trader@example.com",
        payload={"param1": 100, "flag": True},
        status="SUCCESS",
        detail="testing detail",
    )

    log_file = tmp_path / "users" / "user-123" / "action_audit.log"
    assert log_file.exists()
    content = log_file.read_text(encoding="utf-8")
    assert "user_id=user-123" in content
    assert "email=trader@example.com" in content
    assert "action=TEST_ACTION" in content
    assert '"param1": 100' in content
    assert "status=SUCCESS" in content
    assert "detail=testing detail" in content


def test_api_routes_write_action_audit(monkeypatch, tmp_path):
    monkeypatch.setattr("backend.api.DATA_DIR", tmp_path)

    with patch("backend.api.verify_supabase_jwt", return_value={"sub": "user-888", "email": "alice@quant.com"}), \
         patch("backend.api.start_strategy", return_value={"status": "armed"}), \
         patch("backend.api.stop_strategy", return_value={"status": "stopped"}):
        client = TestClient(app)

        # 模擬點擊「啟動策略」按鈕
        resp = client.post(
            "/api/strategy/start?token=fake-token",
            json={
                "strategy_id": "breakout_long",
                "product_code": "TM2609",
                "qty": 1,
                "stop_loss_points": 80.0,
                "take_profit_points": 200.0,
                "max_loss_ntd": 5000.0,
                "max_loss_pct": 0.05,
                "oco_enabled": True,
                "soft_stop_enabled": True,
                "risk_insurance_enabled": True,
                "reverse_signal_exit_enabled": True,
            },
        )
        assert resp.status_code == 200

        # 模擬點擊「停止策略」按鈕
        resp_stop = client.post(
            "/api/strategy/stop?token=fake-token",
            json={"strategy_id": "breakout_long"},
        )
        assert resp_stop.status_code == 200

    log_file = tmp_path / "users" / "user-888" / "action_audit.log"
    assert log_file.exists()
    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "action=START_STRATEGY" in lines[0]
    assert "breakout_long" in lines[0]
    assert "action=STOP_STRATEGY" in lines[1]
