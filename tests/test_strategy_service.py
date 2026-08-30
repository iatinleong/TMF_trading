import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from backend.capital_parse import parse_open_interest_line, parse_order_report_line
from backend import strategy_service
from backend.strategy_service import (
    StrategyState,
    _armed,
    _canonical_position_product,
    _cancel_protection_order_for_state,
    _check_orphaned_fill,
    _place_oco_protection_for_state,
    _settlement_month_from_tmf_code,
    _tick_one,
    reconcile_after_manual_close,
    reconcile_orphan_stop_orders,
    strategy_config_defaults,
    stop_strategy,
)


def _order_row(*, status_code: str, direction: str, qty: int, when: datetime) -> str:
    """組出一筆結構跟真實 orders_raw 相符的委託列（欄位位置對照 capital_parse.py）。"""
    date_part = when.strftime("%Y%m%d")
    time_part = when.strftime("%H%M%S")
    parts = [""] * 44
    parts[0], parts[1], parts[2] = "TF", "FUT", "TAIFEX"
    parts[7] = "test1"
    parts[8] = parts[9] = "1234567890123"
    parts[10] = status_code
    parts[11] = date_part
    parts[12] = time_part
    parts[15] = "TMFH6"
    parts[22] = direction
    parts[27] = "20000.0000"
    parts[30] = str(qty)
    parts[31] = str(qty)
    parts[32] = "0"
    parts[33] = "N"
    parts[43] = "20000.0000"
    return ",".join(parts)


def test_parse_open_interest_line():
    row = parse_open_interest_line("TF,F0200006921941,TMFR1,B,2,0,21500")
    assert row["product"] == "TMFR1"
    assert row["direction_key"] == "long"
    assert row["qty"] == "2"
    assert row["price"] == "21500"


def test_parse_order_report_line():
    # 真實 orders_raw 資料（2026-07-27 從 /api/trading/status 取得），欄位位置已對照
    # 官方文件《4.下單準備介紹.docx》GetOrderReport 58 欄位定義逐一核對過。
    line = (
        "TF,FUT,TAIFEX,F020000,B68,6921941,0000000,y2935,2315605181554,2315605181554,2,"
        "20260727,104232,20260727,20260727,TMFH6,FITM,202608,0,,0,0,S,,,0,2,43706.0000,"
        "43706.0000,1,1,1,0,O,A,N,RI,1,,0041,,0.0000,0,43708.0000,0.0000,0,0.0000,0.0000,0,"
        ",0.0000,0,20260727,104232,,,,,,,,3,1.000000,1.000000,1.000000,0.000000,0.000000,"
        "104232891,,,0,0,,TMFH6,,,"
    )
    row = parse_order_report_line(line)
    assert row["product"] == "TMFH6"
    assert row["seq_no"] == "2315605181554"
    assert row["book_no"] == "y2935"
    assert row["direction_key"] == "short"
    assert row["status_code"] == "2"
    assert row["status"] == "全部成交"
    assert row["qty"] == "1"
    assert row["price"] == "43708.0000"  # 成交均價優先於委託價（市價單委託價欄位為0）
    assert row["is_active"] is False
    # 2026-08-25：委託日期/時間（欄位 11/12）原本完全沒解析出來，「歷史委託」
    # 畫面上看不到任何一筆單是什麼時候下的。
    assert row["time"] == "2026-07-27 10:42:32"


def test_parse_order_report_line_missing_time_fields():
    parts = [""] * 34
    parts[15] = "TMFH6"
    line = ",".join(parts)
    row = parse_order_report_line(line)
    assert row["time"] == ""


def test_strategy_loss_limit():
    st = StrategyState(
        strategy_id="test",
        product_code="TMFR1",
        strategy="ma_cross",
        direction_limit="long",
        label="測試策略",
        initial_capital_ntd=100_000,
        max_loss_ntd=10_000,
        max_loss_pct=0.10,
        realized_pnl_ntd=-10_001,
    )
    assert st.loss_limit_hit()

    st2 = StrategyState(
        strategy_id="test",
        product_code="TMFR1",
        strategy="ma_cross",
        direction_limit="long",
        label="測試策略",
        initial_capital_ntd=100_000,
        max_loss_ntd=10_000,
        max_loss_pct=0.10,
        realized_pnl_ntd=-9_999,
    )
    assert not st2.loss_limit_hit()


def _open_long_state(**overrides) -> StrategyState:
    defaults = dict(
        strategy_id="test",
        product_code="TMFR1",
        strategy="ma_cross",
        direction_limit="long",
        label="測試策略",
        qty=1,
        initial_capital_ntd=100_000,
        max_loss_ntd=10_000,
        max_loss_pct=0.99,  # 拉高比例門檻，測試時只讓金額門檻生效
        held_qty=1,
        held_direction="long",
        entry_price=20000.0,
    )
    defaults.update(overrides)
    return StrategyState(**defaults)


def test_tick_one_forces_close_on_stop_loss_points():
    # entry_price=20000，預設 stop_loss_points=100，報價跌到 19800（跌200點）超過門檻，應強制平倉。
    state = _open_long_state()
    svc = MagicMock()
    svc.place_order.return_value = {"order_result": {"success": True}}
    st = {"quote": {"last_price": 19800.0}}

    _tick_one(state, svc, st)

    svc.place_order.assert_called_once()
    called = svc.place_order.call_args[0][0]
    assert called["side"] == "sell"
    assert called["new_close"] == 1
    assert state.held_qty == 0
    assert state.held_direction is None
    assert state.stopped is True
    assert "停損" in state.stop_reason
    # TMF 每點 10 元，(19800-20000)*10*1 = -2000
    assert state.realized_pnl_ntd == -2000.0


def test_tick_one_forces_close_on_take_profit_points():
    # entry_price=20000，預設 take_profit_points=250，報價漲到 20400（漲400點）超過門檻，應強制停利平倉。
    state = _open_long_state()
    svc = MagicMock()
    svc.place_order.return_value = {"order_result": {"success": True}}
    st = {"quote": {"last_price": 20400.0}}

    _tick_one(state, svc, st)

    svc.place_order.assert_called_once()
    called = svc.place_order.call_args[0][0]
    assert called["side"] == "sell"
    assert state.held_qty == 0
    assert state.stopped is True
    assert "停利" in state.stop_reason
    # TMF 每點 10 元，(20400-20000)*10*1 = 4000
    assert state.realized_pnl_ntd == 4000.0


def test_tick_one_no_stop_loss_within_limit():
    # entry 20000，報價 19950（跌50點），在 stop_loss_points=100 門檻之內，不該平倉、不該停止策略。
    state = _open_long_state()
    svc = MagicMock()
    st = {"quote": {"last_price": 19950.0}}

    _tick_one(state, svc, st)

    svc.place_order.assert_not_called()
    assert state.held_qty == 1
    assert state.stopped is False


def test_tick_one_risk_stop_fallback_triggers_before_point_stop():
    # 2026-08-07 加回的保險：stop_loss_points 設很寬（1000點，不會被點數觸發），
    # 但 max_loss_ntd 設很緊（500元 = 50點），報價跌50點（19950）點數停損還沒到，
    # 但浮動虧損金額已經超過 NTD 門檻，應該要用 risk_stop 觸發強制平倉。
    state = _open_long_state(stop_loss_points=1000.0, take_profit_points=3000.0, max_loss_ntd=500.0, max_loss_pct=0.99)
    svc = MagicMock()
    svc.place_order.return_value = {"order_result": {"success": True}}
    st = {"quote": {"last_price": 19950.0}}

    _tick_one(state, svc, st)

    svc.place_order.assert_called_once()
    assert state.held_qty == 0
    assert state.stopped is True
    assert "風控保險" in state.stop_reason


def test_tick_one_no_risk_stop_when_both_within_limits():
    # 點數、NTD門檻都還沒到，兩邊都不該觸發。
    state = _open_long_state(stop_loss_points=1000.0, take_profit_points=3000.0, max_loss_ntd=500.0, max_loss_pct=0.99)
    svc = MagicMock()
    st = {"quote": {"last_price": 19995.0}}  # 跌5點 = -50元，遠低於500元門檻

    _tick_one(state, svc, st)

    svc.place_order.assert_not_called()
    assert state.held_qty == 1
    assert state.stopped is False


def test_check_orphaned_fill_detects_recent_matching_order():
    row = _order_row(status_code="2", direction="B", qty=1, when=datetime.now())
    live_state = {"orders_raw": row}
    assert _check_orphaned_fill(live_state, expected_side="long", qty=1) is True


def test_check_orphaned_fill_ignores_wrong_direction():
    row = _order_row(status_code="2", direction="B", qty=1, when=datetime.now())
    live_state = {"orders_raw": row}
    assert _check_orphaned_fill(live_state, expected_side="short", qty=1) is False


def test_check_orphaned_fill_ignores_wrong_qty():
    row = _order_row(status_code="2", direction="B", qty=1, when=datetime.now())
    live_state = {"orders_raw": row}
    assert _check_orphaned_fill(live_state, expected_side="long", qty=3) is False


def test_check_orphaned_fill_ignores_old_order():
    old_time = datetime.now() - timedelta(minutes=5)
    row = _order_row(status_code="2", direction="B", qty=1, when=old_time)
    live_state = {"orders_raw": row}
    assert _check_orphaned_fill(live_state, expected_side="long", qty=1) is False


def test_check_orphaned_fill_ignores_no_fill_evidence_status():
    # 狀態碼 3 = 全部取消，沒有成交跡象，不該被當成「其實成功了」。
    row = _order_row(status_code="3", direction="B", qty=1, when=datetime.now())
    live_state = {"orders_raw": row}
    assert _check_orphaned_fill(live_state, expected_side="long", qty=1) is False


def test_check_orphaned_fill_empty_orders_raw():
    assert _check_orphaned_fill({"orders_raw": ""}, expected_side="long", qty=1) is False


def test_tick_one_stop_loss_reports_failure_when_no_orphaned_fill():
    # 平倉真的失敗、且查無相符成交紀錄時，維持原本行為：計入 consecutive_failures，
    # 不清空持倉狀態（因為部位理論上還在）。
    state = _open_long_state()
    svc = MagicMock()
    svc.place_order.return_value = {
        "order_result": {"success": False, "message": "券商拒單"},
        "state": {"orders_raw": ""},
    }
    st = {"quote": {"last_price": 18000.0}}

    _tick_one(state, svc, st)

    assert state.held_qty == 1
    assert state.consecutive_failures == 1
    assert state.stopped is False


def test_tick_one_stop_loss_clears_position_on_orphaned_fill():
    # 平倉委託回報失敗，但實際上 orders_raw 顯示近期已經真的平倉成功——
    # 這裡必須清空 held_qty，否則下一輪會對著不存在的部位再送一次平倉單，
    # 等於意外反手開一個新部位。
    state = _open_long_state()
    fill_row = _order_row(status_code="2", direction="S", qty=1, when=datetime.now())
    svc = MagicMock()
    svc.place_order.return_value = {
        "order_result": {"success": False, "message": "逾時"},
        "state": {"orders_raw": fill_row},
    }
    st = {"quote": {"last_price": 18000.0}}

    _tick_one(state, svc, st)

    assert state.held_qty == 0
    assert state.held_direction is None
    assert state.stopped is True
    assert "已清空" in state.stop_reason


def test_tick_one_stop_loss_runs_even_if_already_stopped():
    # 策略已經因為其他原因被停止，但手上還有留倉時，點數停損仍要保護這筆單子。
    state = _open_long_state(stopped=True, stop_reason="之前已停止")
    svc = MagicMock()
    svc.place_order.return_value = {"order_result": {"success": True}}
    st = {"quote": {"last_price": 18000.0}}

    _tick_one(state, svc, st)

    svc.place_order.assert_called_once()
    assert state.held_qty == 0
    assert "停損" in state.stop_reason


@pytest.fixture(autouse=False)
def clean_armed():
    """`_armed` 是模組全域狀態，測試前後都要清空，避免污染其他測試。"""
    _armed.clear()
    yield _armed
    _armed.clear()


@pytest.fixture(autouse=True)
def _isolate_reconcile_audit_log(tmp_path, monkeypatch):
    """
    2026-08-27：reconcile_after_manual_close 每次評估都會寫稽核 log（見
    _append_reconcile_audit），這裡隔離到 tmp_path，避免測試假資料混進
    專案真實的 data/reconcile_audit.log。
    """
    monkeypatch.setattr(
        strategy_service, "_reconcile_audit_log_path", lambda: tmp_path / "reconcile_audit.log"
    )


def test_stop_strategy_keeps_monitoring_when_holding_position(clean_armed):
    # 2026-08-21 修正：手上還有留倉時，手動關開關不能把策略整個從監控清單移除，
    # 否則停損停利保護會直接消失（沒有真正的券商端停損單在保護）。
    state = _open_long_state(strategy_id="breakout_long")
    _armed["breakout_long"] = state

    result = stop_strategy("breakout_long")

    assert "breakout_long" in _armed
    assert _armed["breakout_long"].held_qty == 1
    assert result["armed"] is False
    assert result["stopped"] is True
    assert "留倉" in result["stop_reason"]


def test_stop_strategy_removes_when_flat(clean_armed):
    state = _open_long_state(strategy_id="breakout_long", held_qty=0, held_direction=None)
    _armed["breakout_long"] = state

    result = stop_strategy("breakout_long")

    assert "breakout_long" not in _armed
    assert result["armed"] is False


def test_reconcile_after_manual_close_syncs_mismatched_strategy_without_stopping(clean_armed):
    """
    手動平倉之後，內部記錄的口數總和（1）超過券商實際回報的淨部位（0），
    代表這筆持倉剛才被外部（手動平倉，或真實保護單觸發）關掉了，策略自己
    不知道，要同步內部記帳。

    2026-08-29 事後討論：原本這裡還會停止策略、要求人工重啟——但確認過
    這個專案實務上不會多策略共用同一商品同一方向，且券商本身不允許同一
    商品雙向持倉，走到這裡的成因只剩手動平倉／保護單觸發，都是正常事件，
    不該停止監控。broker 才是唯一真相，這裡只是同步，不是「出事了」。
    """
    state = _open_long_state(strategy_id="breakout_long")
    _armed["breakout_long"] = state

    with patch.object(strategy_service.TradingService, "get") as mock_get:
        mock_get.return_value.status.return_value = {"positions": []}
        affected = reconcile_after_manual_close("TMFR1")

    assert affected == ["breakout_long"]
    assert state.held_qty == 0
    assert state.held_direction is None
    assert state.stopped is False
    assert "不一致" in state.last_action


def test_reconcile_after_manual_close_leaves_matching_strategy_alone(clean_armed):
    # 券商實際部位跟內部記錄的口數對得起來，不該動它。
    state = _open_long_state(strategy_id="breakout_long")
    _armed["breakout_long"] = state

    with patch.object(strategy_service.TradingService, "get") as mock_get:
        mock_get.return_value.status.return_value = {
            "positions": [
                {"product": "TMFR1", "direction_key": "long", "qty": "1"}
            ]
        }
        affected = reconcile_after_manual_close("TMFR1")

    assert affected == []
    assert state.held_qty == 1
    assert state.stopped is False


def test_strategy_config_defaults_use_confirmed_stop_take_points():
    # 2026-08-27：跟使用者核對過的正式規格是 -100 停損／+250 停利（不是程式碼
    # 原本寫死的 300）——這裡鎖住這組數字，避免以後改動又不小心跑掉。
    defaults = strategy_config_defaults("breakout_long")
    assert defaults["stop_loss_points"] == 100.0
    assert defaults["take_profit_points"] == 250.0


def test_canonical_position_product_strips_year_from_tmf_month_code():
    assert _canonical_position_product("TM2609") == "TM09"


def test_canonical_position_product_leaves_already_short_code_unchanged():
    assert _canonical_position_product("TM09") == "TM09"


def test_canonical_position_product_leaves_rolling_code_unchanged():
    assert _canonical_position_product("TMFR1") == "TMFR1"


def test_reconcile_after_manual_close_matches_broker_short_month_product_code(clean_armed):
    """
    2026-08-27 實盤事故：策略內部用 state.product_code="TM2609"（具體月份碼）
    記帳，但群益 OnOpenInterest（GetOpenInterestGW）回報的 position["product"]
    對同一張倉位卻是 "TM09"（不含年份的短碼）——見
    backend/capital_parse.py::parse_open_interest_line。這個函式原本直接拿
    兩邊字串比對，永遠對不起來，把一筆真實、完全對得上的持倉誤判成「內部
    記錄超過券商實際回報」，觸發自動撤單（連已經成功掛上的真實 STP 停損智慧
    單都被撤掉），留下裸倉。這裡驗證同一張倉位、只是產品代碼格式不同，不該
    被誤判成不一致。
    """
    state = _open_long_state(strategy_id="pullback_long", product_code="TM2609")
    _armed["pullback_long"] = state

    with patch.object(strategy_service.TradingService, "get") as mock_get:
        mock_get.return_value.status.return_value = {
            "positions": [
                {"product": "TM09", "direction_key": "long", "qty": "1"}
            ]
        }
        affected = reconcile_after_manual_close("TM2609")

    assert affected == []
    assert state.held_qty == 1
    assert state.stopped is False


def test_reconcile_after_manual_close_writes_audit_log_on_mismatch(clean_armed, tmp_path):
    """
    2026-08-27：使用者質疑「為什麼你不把重要動作都 logging，就不用猜來猜去」
    ——reconcile_after_manual_close 判定「內部記錄跟券商回報不一致」這麼關鍵
    的決定，之前完全沒有留下任何看得到的紀錄（只能事後從 SKCOM 自己的原始
    log 側面猜測，猜不準）。這裡驗證每次評估都要把實際比對的數字寫進稽核
    log，下次再發生類似狀況能直接查到根因，不用再猜。
    """
    state = _open_long_state(strategy_id="breakout_long", product_code="TM2609")
    _armed["breakout_long"] = state

    with patch.object(strategy_service.TradingService, "get") as mock_get:
        mock_get.return_value.status.return_value = {"positions": []}
        reconcile_after_manual_close("TM2609")

    log_path = tmp_path / "reconcile_audit.log"
    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "product=TM09" in content
    assert "direction=long" in content
    assert "internal_total=1" in content
    assert "actual=0" in content
    assert "mismatch=True" in content
    assert "affected=breakout_long" in content


def test_reconcile_after_manual_close_writes_audit_log_when_matching(clean_armed, tmp_path):
    """對得起來的情況也要留紀錄（mismatch=False），才能證明「這一輪有查過、
    沒問題」，而不是「這一輪根本沒查」。"""
    state = _open_long_state(strategy_id="breakout_long", product_code="TM2609")
    _armed["breakout_long"] = state

    with patch.object(strategy_service.TradingService, "get") as mock_get:
        mock_get.return_value.status.return_value = {
            "positions": [{"product": "TM09", "direction_key": "long", "qty": "1"}]
        }
        reconcile_after_manual_close("TM2609")

    log_path = tmp_path / "reconcile_audit.log"
    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "mismatch=False" in content
    assert "internal_total=1" in content
    assert "actual=1" in content


def test_reconcile_after_manual_close_skips_freshly_entered_state(clean_armed, tmp_path):
    """
    2026-08-29 實盤事故：22:00:23 掛好的真實 OCO 保護單，22:00:26（只隔 3 秒）
    就被 reconcile_after_manual_close 誤判撤掉，部位裸奔超過 14 小時。根因是
    svc.status() 讀的是快取（by OnOpenInterest 事件被動更新），不是重新查詢——
    剛進場那一刻，快取還沒跟上這筆新單，reconcile 立刻拿這份過期快照去比對，
    一定會誤判「內部有、券商沒有」。改成呼叫 svc.refresh() 逼它重查也救不了，
    因為下單流程自己也會呼叫 refresh_live_snapshot()，而那個函式有群益 M999
    限制的 5 秒節流，3 秒內的重查請求會被節流擋掉、一樣拿到過期資料。

    真正的修法：剛進場（entry_recorded_at 在 30 秒內）的部位，先跳過這一輪
    的核對，留給 broker 端的快照時間跟上，不要立刻拿去比對。
    """
    state = _open_long_state(
        strategy_id="breakout_long",
        product_code="TM2609",
        entry_recorded_at=time.time(),  # 剛剛才進場
    )
    _armed["breakout_long"] = state

    with patch.object(strategy_service.TradingService, "get") as mock_get:
        # 券商快照還沒跟上，回報空倉——如果沒有寬限期保護，會被誤判成不一致。
        mock_get.return_value.status.return_value = {"positions": []}
        affected = reconcile_after_manual_close("TM2609")

    assert affected == []
    assert state.held_qty == 1
    assert state.held_direction == "long"
    assert state.stopped is False


def test_reconcile_after_manual_close_still_reconciles_after_grace_period_expires(
    clean_armed, tmp_path
):
    """寬限期過了之後，該抓的不一致還是要抓——寬限期是延後核對，不是永久跳過。"""
    state = _open_long_state(
        strategy_id="breakout_long",
        product_code="TM2609",
        entry_recorded_at=time.time() - 31.0,  # 31 秒前進場，寬限期（30 秒）已過
    )
    _armed["breakout_long"] = state

    with patch.object(strategy_service.TradingService, "get") as mock_get:
        mock_get.return_value.status.return_value = {"positions": []}
        affected = reconcile_after_manual_close("TM2609")

    assert affected == ["breakout_long"]
    assert state.held_qty == 0
    assert state.stopped is False


def test_reconcile_after_manual_close_does_not_cancel_protection_order(clean_armed):
    """
    2026-08-29 實盤事故：這裡原本會順手撤掉「看起來變成孤兒」的保護單——但
    那次事故證明這個比對可能建立在過期快照上，一旦誤判，撤單是不可逆的，
    會把一筆其實還在的真實部位的保護整個拿掉（22:00:19 進場、22:00:26
    OCO 就被這個機制撤掉，部位裸奔超過 18 小時）。改成：清空內部記帳同步
    現實，繼續正常監控（不停止策略、也不碰券商端的保護單）——就算這次
    判斷是誤判，保護單不受影響，真正的部位依然受到保護。真正孤兒的保護
    單交給 reconcile_orphan_stop_orders 處理（那裡對剛進場的部位也有寬限
    期保護，見該函式）。
    """
    state = _open_long_state(
        strategy_id="breakout_long",
        protection_order_smart_key="26462382",
    )
    _armed["breakout_long"] = state

    with patch.object(strategy_service.TradingService, "get") as mock_get:
        mock_get.return_value.status.return_value = {"positions": []}
        affected = reconcile_after_manual_close("TMFR1")

    assert affected == ["breakout_long"]
    assert state.held_qty == 0
    assert state.stopped is False
    # 保護單完全沒被動到——這是這次修復的核心：清帳跟撤單分開，不可逆的
    # 動作不再由這個比對直接觸發。
    assert state.protection_order_smart_key == "26462382"
    mock_get.return_value.cancel_stop_order.assert_not_called()


def test_strategy_tick_runs_position_reconciliation_every_cycle(clean_armed):
    # strategy_tick() 現在每輪都要主動核對一次 broker 實際部位，不能只靠手動
    # 平倉按鈕才觸發，不然 STP/MIT 在券商端自己觸發時永遠不會被發現。
    state = _open_long_state(strategy_id="breakout_long")
    _armed["breakout_long"] = state

    with patch.object(strategy_service.TradingService, "get") as mock_get, patch.object(
        strategy_service, "reconcile_after_manual_close"
    ) as mock_reconcile:
        mock_get.return_value.status.return_value = {"connected": True, "positions": []}
        mock_reconcile.return_value = []
        strategy_service.strategy_tick()

    mock_reconcile.assert_called_once()


def test_contract_from_product_recognizes_real_tmf_month_codes():
    # 2026-08-21 修正：實際活著在用的商品碼是 "TM"+YYMM（例如 "TM2609"），
    # 原本只認 "TMF" 前綴會誤判成 TX（point_value 200，正確應該是 TMF 的 10）。
    from backend.strategy_service import _contract_from_product

    assert _contract_from_product("TM2609") == "TMF"
    assert _contract_from_product("TM2608") == "TMF"
    assert _contract_from_product("TMFR1") == "TMF"
    assert _contract_from_product("MXFR1") == "MTX"
    assert _contract_from_product("TX00") == "TX"


def test_settlement_month_from_tmf_code():
    assert _settlement_month_from_tmf_code("TM2609") == "202609"
    assert _settlement_month_from_tmf_code("tm2609") == "202609"


def test_settlement_month_from_tmf_code_rejects_non_month_codes():
    # TMFR1/TX00/MXFR1 這類連續代碼沒有具體月份可以推導，STP 目前只驗證過 TMF。
    assert _settlement_month_from_tmf_code("TMFR1") is None
    assert _settlement_month_from_tmf_code("TX00") is None
    assert _settlement_month_from_tmf_code("MXFR1") is None


def test_bars_from_kline_store_uses_taipei_local_time_not_utc():
    """
    2026-08-30 code review 抓到的第 4 次「naive 時間戳被當成 UTC」bug（前三次
    見 docs/vm_deployment_gotchas.md 第 5 節）：`get_store().get_klines()` 存的
    `"time"` 是用 `timeutil.to_unix_seconds()` 算出來的真正 UTC 秒數（已經正確
    處理過台北時區），但 `_bars_from_kline_store` 用
    `pd.to_datetime(frame["time"], unit="s")` 直接還原，沒有再轉回 Asia/Taipei，
    等於把台北時間 13:45 的 K 棒標成 05:45（差 8 小時）——已用真實計算驗證過：
    ``to_unix_seconds(pd.Timestamp('2026-08-28 13:45:00'))`` 算出來的 epoch，
    拿去 ``pd.to_datetime(epoch, unit='s')`` 還原出來是 05:45:00，不是 13:45:00。
    這裡驗證修好之後，還原出來的索引時間要跟原始台北時間一致。
    """
    from backend.timeutil import to_unix_seconds

    taipei_close_time = pd.Timestamp("2026-08-28 13:45:00")
    epoch = to_unix_seconds(taipei_close_time)
    fake_klines = [
        {
            "time": epoch,
            "open": 46300.0,
            "high": 46360.0,
            "low": 46250.0,
            "close": 46300.0,
            "volume": 100,
        }
    ]

    fake_store = MagicMock()
    fake_store.get_klines.return_value = fake_klines

    with patch.object(strategy_service, "get_store", return_value=fake_store):
        bars = strategy_service._bars_from_kline_store("TM2609")

    assert bars.index[0] == taipei_close_time


def _oco_success_response() -> dict:
    return {
        "order_result": {
            "success": True,
            "raw": "20260827,二擇一委託已送出 條件單號：26564233,s5209,26564233,1688100001094",
        }
    }


def test_place_oco_protection_for_state_long_orders_legs_by_value():
    # 2026-08-27：OCO 第一腳觸發價必須比第二腳高（實測驗證，見
    # _place_oco_protection_for_state 說明）。多單的停利（entry+250）比停損
    # （entry-100）高，所以第一腳應該是停利、第二腳是停損。
    state = _open_long_state(product_code="TM2609", entry_price=20000.0)
    svc = MagicMock()
    svc.place_oco_order.return_value = _oco_success_response()

    _place_oco_protection_for_state(state, svc)

    assert state.protection_order_smart_key == "26564233"
    assert state.protection_order_order_no == "s5209"
    assert state.protection_order_seq_no == "1688100001094"
    call_kwargs = svc.place_oco_order.call_args.args[0]
    assert call_kwargs["side"] == "sell"  # 保護多單，兩腳都是賣出平倉
    assert call_kwargs["side2"] == "sell"
    assert call_kwargs["trigger_price"] == "20250"  # 第一腳：停利（較高）
    assert call_kwargs["trigger_price2"] == "19900"  # 第二腳：停損（較低）
    assert call_kwargs["settlement_month"] == "202609"
    assert call_kwargs["new_close"] == "close"
    assert call_kwargs["order_price_type"] == 2


def test_place_oco_protection_for_state_short_flips_leg_order():
    # 空單的停損（entry+100）比停利（entry-250）高，所以第一腳變成停損、
    # 第二腳變成停利——跟多單剛好相反，因為排序是照數值大小，不是照
    # 「哪一腳是停損/停利」的語意。
    state = _open_long_state(
        product_code="TM2609", held_direction="short", entry_price=20000.0
    )
    svc = MagicMock()
    svc.place_oco_order.return_value = _oco_success_response()

    _place_oco_protection_for_state(state, svc)

    call_kwargs = svc.place_oco_order.call_args.args[0]
    assert call_kwargs["side"] == "buy"  # 保護空單，兩腳都是買進平倉
    assert call_kwargs["side2"] == "buy"
    assert call_kwargs["trigger_price"] == "20100"  # 第一腳：停損（較高）
    assert call_kwargs["trigger_price2"] == "19750"  # 第二腳：停利（較低）


def test_place_oco_protection_for_state_failure_leaves_no_tracking():
    state = _open_long_state(product_code="TM2609")
    svc = MagicMock()
    svc.place_oco_order.return_value = {
        "order_result": {"success": False, "message": "風控失敗"}
    }

    _place_oco_protection_for_state(state, svc)

    assert state.protection_order_smart_key is None
    assert "改用軟停損停利保護" in state.last_action


def test_place_oco_protection_for_state_skips_non_tmf_product():
    state = _open_long_state(product_code="TMFR1")
    svc = MagicMock()

    _place_oco_protection_for_state(state, svc)

    svc.place_oco_order.assert_not_called()
    assert state.protection_order_smart_key is None


def test_cancel_protection_order_for_state_success_clears_tracking():
    state = _open_long_state(
        product_code="TM2609",
        protection_order_smart_key="26564233",
        protection_order_seq_no="1688100001094",
        protection_order_order_no="s5209",
    )
    svc = MagicMock()
    svc.cancel_stop_order.return_value = {"cancel_result": {"success": True}}

    _cancel_protection_order_for_state(state, svc)

    assert state.protection_order_smart_key is None
    assert state.protection_order_seq_no == ""
    assert state.protection_order_order_no == ""
    call_kwargs = svc.cancel_stop_order.call_args.args[0]
    assert call_kwargs["trade_kind"] == 3  # OCO，見 SmartOrderKind.OCO


def test_cancel_protection_order_for_state_failure_keeps_tracking_for_retry():
    state = _open_long_state(product_code="TM2609", protection_order_smart_key="26564233")
    svc = MagicMock()
    svc.cancel_stop_order.return_value = {"cancel_result": {"success": False}}

    _cancel_protection_order_for_state(state, svc)

    assert state.protection_order_smart_key == "26564233"


def test_cancel_protection_order_for_state_noop_when_nothing_tracked():
    state = _open_long_state(product_code="TM2609", protection_order_smart_key=None)
    svc = MagicMock()

    _cancel_protection_order_for_state(state, svc)

    svc.cancel_stop_order.assert_not_called()


def test_tick_one_skips_soft_stop_loss_when_broker_protection_order_exists(clean_armed):
    # 有真正的 OCO 保護單頂著時，軟停損檢查要讓路，不要兩邊搶著平倉。
    # 用 150 點跌幅：超過預設 stop_loss_points=100（若軟停損沒讓路會觸發），
    # 但用正確的 TMF point_value=10 換算浮動損益只有 1,500 元，不會誤觸風控保險。
    state = _open_long_state(product_code="TM2609", protection_order_smart_key="26564233")
    svc = MagicMock()
    st = {"quote": {"last_price": 19850.0}}

    _tick_one(state, svc, st)

    svc.place_order.assert_not_called()
    assert state.held_qty == 1  # 沒被軟停損平掉


def test_tick_one_entry_places_oco_protection(clean_armed):
    state = _open_long_state(
        product_code="TM2609", held_qty=0, held_direction=None, entry_price=0.0
    )
    state.last_signal_key = None
    svc = MagicMock()
    svc.place_order.return_value = {"order_result": {"success": True}}
    svc.place_oco_order.return_value = _oco_success_response()
    st = {"quote": {"last_price": 20000.0}}

    fake_bars = MagicMock()
    fake_bars.empty = False
    fake_bars.__len__.return_value = 100

    with patch.object(
        strategy_service,
        "_latest_closed_signal",
        return_value={"time": "2026-08-21T09:45:00", "direction": "long", "key": "k1"},
    ), patch.object(strategy_service, "_load_bars", return_value=fake_bars), patch.object(
        strategy_service, "_signaled_frame", return_value=MagicMock()
    ):
        _tick_one(state, svc, st)

    assert state.held_qty == 1
    svc.place_oco_order.assert_called_once()
    assert state.protection_order_smart_key == "26564233"


def test_tick_one_entry_falls_back_to_bar_close_when_quote_missing(clean_armed):
    """
    2026-08-30 code review 抓到的隱患：進場成交那一刻，如果即時報價剛好缺失
    （`st["quote"]["last_price"]` 是 None，過去實測過的 3003 報價連線問題就
    可能造成這種情況），原本的寫法 `entry_px = float(quote) if quote else 0.0`
    會直接把 entry_price 設成 0.0——因為 `_place_oco_protection_for_state` 只在
    `state.entry_price > 0` 才會呼叫，這會讓真正的 OCO 保護單完全被跳過，
    軟停損停利也會因為 entry_price=0 算不出正確點數而失效，變成整筆進場
    完全沒有任何保護。這裡驗證：報價缺失時要退回用最新收盤 K 棒的收盤價
    當進場價估計值，而不是直接放棄成 0。
    """
    state = _open_long_state(
        product_code="TM2609", held_qty=0, held_direction=None, entry_price=0.0
    )
    state.last_signal_key = None
    svc = MagicMock()
    svc.place_order.return_value = {"order_result": {"success": True}}
    svc.place_oco_order.return_value = _oco_success_response()
    st = {"quote": {"last_price": None}}  # 即時報價缺失

    # _tick_one 進場前會檢查 len(bars) >= DEFAULT_STRATEGY.ma_slow + 2，這裡湊
    # 足夠的根數，只有最後一根（最新收盤）的收盤價 20200.0 是這個測試在意的。
    bar_count = strategy_service.DEFAULT_STRATEGY.ma_slow + 2
    fake_bars = pd.DataFrame(
        {
            "open": [20000.0] * bar_count,
            "high": [20050.0] * bar_count,
            "low": [19950.0] * bar_count,
            "close": [20000.0] * (bar_count - 1) + [20200.0],
        }
    )

    with patch.object(
        strategy_service,
        "_latest_closed_signal",
        return_value={"time": "2026-08-21T09:45:00", "direction": "long", "key": "k1"},
    ), patch.object(strategy_service, "_load_bars", return_value=fake_bars), patch.object(
        strategy_service, "_signaled_frame", return_value=MagicMock()
    ):
        _tick_one(state, svc, st)

    assert state.held_qty == 1
    assert state.entry_price == 20200.0  # 退回用最新收盤價，不是 0
    svc.place_oco_order.assert_called_once()  # 有 entry_price > 0，OCO 保護單才會真的掛上


def test_tick_one_entry_records_estimated_trade_on_orphaned_fill(clean_armed):
    # 2026-08-25：進場委託回報失敗、但 orders_raw 顯示近期有相符的真實成交紀錄時，
    # 原本完全不會呼叫 _record_trade()，圖表上看不到這筆疑似進場——使用者得自己
    # 去對帳才知道發生過什麼事。這裡改成補記一筆（價格用當下報價估計），但不動
    # held_qty/held_direction（那些欄位仍然刻意不猜測歸屬哪個策略）。
    state = _open_long_state(
        strategy_id="breakout_long_test",
        product_code="TM2609",
        held_qty=0,
        held_direction=None,
        entry_price=0.0,
    )
    state.last_signal_key = None
    fill_row = _order_row(status_code="2", direction="B", qty=1, when=datetime.now())
    svc = MagicMock()
    svc.place_order.return_value = {
        "order_result": {"success": False, "message": "逾時"},
        "state": {"orders_raw": fill_row},
    }
    st = {"quote": {"last_price": 20000.0}}

    fake_bars = MagicMock()
    fake_bars.empty = False
    fake_bars.__len__.return_value = 100

    strategy_service._trade_log.pop("breakout_long_test", None)
    try:
        with patch.object(
            strategy_service,
            "_latest_closed_signal",
            return_value={"time": "2026-08-25T09:45:00", "direction": "long", "key": "k1"},
        ), patch.object(strategy_service, "_load_bars", return_value=fake_bars), patch.object(
            strategy_service, "_signaled_frame", return_value=MagicMock()
        ):
            _tick_one(state, svc, st)

        # held_qty 不能被這個分支動到：這筆單無法安全歸屬給哪個策略。
        assert state.held_qty == 0
        assert state.held_direction is None
        assert state.stopped is True

        trades = strategy_service._trade_log.get("breakout_long_test", [])
        assert len(trades) == 1
        assert trades[0]["type"] == "entry"
        assert trades[0]["direction"] == "long"
        assert trades[0]["price"] == 20000.0
    finally:
        strategy_service._trade_log.pop("breakout_long_test", None)


def test_reconcile_orphan_stop_orders_cleans_flat_positions(clean_armed):
    state = _open_long_state(
        strategy_id="breakout_long",
        product_code="TM2609",
        held_qty=0,
        held_direction=None,
        protection_order_smart_key="26564233",
    )
    _armed["breakout_long"] = state

    with patch.object(strategy_service.TradingService, "get") as mock_get:
        mock_get.return_value.cancel_stop_order.return_value = {
            "cancel_result": {"success": True}
        }
        cleaned = reconcile_orphan_stop_orders()

    assert cleaned == ["breakout_long"]
    assert state.protection_order_smart_key is None


def test_reconcile_orphan_stop_orders_ignores_held_positions(clean_armed):
    state = _open_long_state(
        strategy_id="breakout_long",
        product_code="TM2609",
        protection_order_smart_key="26564233",
    )  # held_qty=1 (預設)，代表這張 OCO 保護單還在保護，不是孤兒單
    _armed["breakout_long"] = state
    svc_get_patch = patch.object(strategy_service.TradingService, "get")

    with svc_get_patch as mock_get:
        cleaned = reconcile_orphan_stop_orders()
        mock_get.return_value.cancel_stop_order.assert_not_called()

    assert cleaned == []
    assert state.protection_order_smart_key == "26564233"


def test_reconcile_orphan_stop_orders_respects_entry_grace_period(clean_armed):
    """
    2026-08-29：如果 held_qty==0 是 reconcile_after_manual_close 在寬限期內
    誤判清空的，entry_recorded_at 還是剛剛的時間——這裡如果照樣撤單，等於
    繞過寬限期，讓那邊拿掉自動撤單的修復完全白做。確認寬限期內即使
    held_qty==0 也不會撤。
    """
    state = _open_long_state(
        strategy_id="breakout_long",
        held_qty=0,
        held_direction=None,
        protection_order_smart_key="26564233",
        entry_recorded_at=time.time(),  # 剛剛才進場（後來被誤判清空）
    )
    _armed["breakout_long"] = state

    with patch.object(strategy_service.TradingService, "get") as mock_get:
        cleaned = reconcile_orphan_stop_orders()
        mock_get.return_value.cancel_stop_order.assert_not_called()

    assert cleaned == []
    assert state.protection_order_smart_key == "26564233"


def test_reconcile_orphan_stop_orders_cleans_up_after_grace_period_expires(clean_armed):
    """寬限期過了之後，真正的孤兒單還是要清掉——寬限期是延後，不是永久跳過。"""
    state = _open_long_state(
        strategy_id="breakout_long",
        held_qty=0,
        held_direction=None,
        protection_order_smart_key="26564233",
        entry_recorded_at=time.time() - 31.0,  # 寬限期（30 秒）已過
    )
    _armed["breakout_long"] = state

    with patch.object(strategy_service.TradingService, "get") as mock_get:
        mock_get.return_value.cancel_stop_order.return_value = {
            "cancel_result": {"success": True}
        }
        cleaned = reconcile_orphan_stop_orders()

    assert cleaned == ["breakout_long"]
    assert state.protection_order_smart_key is None


def test_reconcile_orphan_stop_orders_respects_held_qty_cleared_grace_period(clean_armed):
    """
    2026-08-29 Gemini review 抓到的漏洞：reconcile_after_manual_close 只有在
    entry_recorded_at 的寬限期已經過了才會清零 held_qty，所以走到這裡時，
    entry_recorded_at 算出來的寬限期一定也已經過期——如果這裡沿用同一個
    時間戳，形同沒有寬限期，同一輪就會立刻撤單，寬限期完全沒發揮作用。
    這裡驗證：就算 entry_recorded_at 早就過了寬限期，只要 held_qty 是
    「剛剛」才被清零的（held_qty_cleared_at 在寬限期內），還是要跳過撤單。
    """
    state = _open_long_state(
        strategy_id="breakout_long",
        held_qty=0,
        held_direction=None,
        protection_order_smart_key="26564233",
        entry_recorded_at=time.time() - 60.0,  # 進場寬限期早就過了
        held_qty_cleared_at=time.time(),  # 但 held_qty 是剛剛才被清零的
    )
    _armed["breakout_long"] = state

    with patch.object(strategy_service.TradingService, "get") as mock_get:
        cleaned = reconcile_orphan_stop_orders()
        mock_get.return_value.cancel_stop_order.assert_not_called()

    assert cleaned == []
    assert state.protection_order_smart_key == "26564233"


def test_reconcile_orphan_stop_orders_cleans_up_after_cleared_grace_period_expires(
    clean_armed,
):
    """held_qty 被清零之後，寬限期真的過了，孤兒單還是要清掉。"""
    state = _open_long_state(
        strategy_id="breakout_long",
        held_qty=0,
        held_direction=None,
        protection_order_smart_key="26564233",
        entry_recorded_at=time.time() - 60.0,
        held_qty_cleared_at=time.time() - 31.0,  # 清零後的寬限期也過了
    )
    _armed["breakout_long"] = state

    with patch.object(strategy_service.TradingService, "get") as mock_get:
        mock_get.return_value.cancel_stop_order.return_value = {
            "cancel_result": {"success": True}
        }
        cleaned = reconcile_orphan_stop_orders()

    assert cleaned == ["breakout_long"]
    assert state.protection_order_smart_key is None


def test_reconcile_after_manual_close_sets_held_qty_cleared_at(clean_armed):
    """reconcile_after_manual_close 清零時要記下時間戳，給 reconcile_orphan_stop_orders 起算獨立寬限期用。"""
    state = _open_long_state(strategy_id="breakout_long")
    _armed["breakout_long"] = state

    with patch.object(strategy_service.TradingService, "get") as mock_get:
        mock_get.return_value.status.return_value = {"positions": []}
        before = time.time()
        reconcile_after_manual_close("TMFR1")
        after = time.time()

    assert state.held_qty_cleared_at is not None
    assert before <= state.held_qty_cleared_at <= after


def test_tick_one_skips_soft_take_profit_when_broker_protection_order_exists(clean_armed):
    # 有真正的 OCO 停損停利保護單頂著時，軟停利檢查要讓路，不要兩邊搶著平倉。
    state = _open_long_state(product_code="TM2609", protection_order_smart_key="26564233")
    svc = MagicMock()
    st = {"quote": {"last_price": 20350.0}}  # 進場價 20000 + 350 > 預設 take_profit_points=250

    _tick_one(state, svc, st)

    svc.place_order.assert_not_called()
    assert state.held_qty == 1