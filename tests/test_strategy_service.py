import json
import time
from datetime import datetime, timedelta
from datetime import time as dt_time
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from backend.broker.capital_futures import CapitalFuturesBroker
from backend.capital_parse import parse_open_interest_line, parse_order_report_line
from backend import strategy_service
from backend.strategy_service import (
    StrategyState,
    _armed,
    _canonical_position_product,
    _cancel_protection_order_for_state,
    _check_orphaned_fill,
    _load_bars,
    _opposite_direction_position_exists,
    _place_oco_protection_for_state,
    _settlement_month_from_tmf_code,
    _tick_one,
    klines_with_signals,
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


def test_strategy_state_layer_toggles_default_to_true():
    state = _open_long_state()
    assert state.oco_enabled is True
    assert state.soft_stop_enabled is True
    assert state.risk_insurance_enabled is True
    assert state.reverse_signal_exit_enabled is True


def test_strategy_config_defaults_includes_layer_toggles():
    defaults = strategy_config_defaults("breakout_long")
    assert defaults["oco_enabled"] is True
    assert defaults["soft_stop_enabled"] is True
    assert defaults["risk_insurance_enabled"] is True
    assert defaults["reverse_signal_exit_enabled"] is True


def test_state_to_dict_includes_layer_toggles():
    state = _open_long_state(oco_enabled=False, reverse_signal_exit_enabled=False)
    result = strategy_service._state_to_dict(state)
    assert result["oco_enabled"] is False
    assert result["soft_stop_enabled"] is True
    assert result["risk_insurance_enabled"] is True
    assert result["reverse_signal_exit_enabled"] is False


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


def test_tick_one_ignores_quote_when_product_mismatches_state_product():
    """
    2026-09-30 抓到的架構缺陷：群益報價訂閱同時只能訂閱一個商品，所有已武裝的
    策略共用同一份 st["quote"]。如果操作者在 Dashboard 把商品代碼框切去看別的
    商品（例如切到大台 TX00），這個策略明明是 TMF，卻會拿到 TX00 的報價，
    當作自己的現價去判斷停損/停利——TX/TMF 都追蹤台指、價位接近，這個錯誤
    不會一眼看出來是錯的，比完全沒有報價更危險。這裡驗證：quote 裡帶的
    product_code 跟這個策略自己的商品不是同一種合約類型時，必須完全不信任
    這個報價（等同沒收到報價），不能觸發停損平倉——即使報的價格數字本身，
    照原本的（錯誤）邏輯算下去確實會超過停損門檻。
    """
    state = _open_long_state()  # product_code="TMFR1" -> contract "TMF"
    svc = MagicMock()
    # 19800 若真的被信任為 TMF 現價，會超過 stop_loss_points=100 門檻觸發強制平倉；
    # 這裡驗證即使報價數字看起來會觸發，只要來源商品對不上，也不能真的動作。
    st = {"quote": {"last_price": 19800.0, "product_code": "TXFH6"}}

    _tick_one(state, svc, st)

    svc.place_order.assert_not_called()
    assert state.held_qty == 1
    assert state.stopped is False


def test_tick_one_uses_quote_when_product_matches_state_product():
    """跟上面那個測試對照組：product_code 有帶、而且跟這個策略自己的商品是
    同一種合約類型（TMF）時，必須正常信任這份報價、照常觸發停損——證明這個
    防呆不是無條件地把有 product_code 的報價都擋掉，只擋真的對不上的。"""
    state = _open_long_state()  # product_code="TMFR1" -> contract "TMF"
    svc = MagicMock()
    svc.place_order.return_value = {"order_result": {"success": True}}
    st = {"quote": {"last_price": 19800.0, "product_code": "TM2610"}}

    _tick_one(state, svc, st)

    svc.place_order.assert_called_once()
    assert state.held_qty == 0
    assert state.stopped is True
    assert "停損" in state.stop_reason


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


def test_tick_one_soft_stop_skipped_when_disabled():
    # 跟 test_tick_one_forces_close_on_stop_loss_points 同一組價位（跌 200
    # 點，遠超預設 stop_loss_points=100），但 soft_stop_enabled=False 時
    # 不該觸發平倉。max_loss_ntd 拉高避免順便撞到風控保險。
    state = _open_long_state(soft_stop_enabled=False, max_loss_ntd=100_000.0)
    svc = MagicMock()
    st = {"quote": {"last_price": 19800.0}}

    _tick_one(state, svc, st)

    svc.place_order.assert_not_called()
    assert state.held_qty == 1
    assert state.stopped is False


def test_tick_one_risk_insurance_skipped_when_disabled():
    # 跟 test_tick_one_risk_stop_fallback_triggers_before_point_stop 同一組
    # 場景（點數停損門檻拉很寬，浮動虧損金額超過 max_loss_ntd），但
    # risk_insurance_enabled=False 時不該觸發平倉。
    state = _open_long_state(
        stop_loss_points=1000.0, take_profit_points=3000.0,
        max_loss_ntd=500.0, max_loss_pct=0.99,
        risk_insurance_enabled=False,
    )
    svc = MagicMock()
    st = {"quote": {"last_price": 19950.0}}

    _tick_one(state, svc, st)

    svc.place_order.assert_not_called()
    assert state.held_qty == 1
    assert state.stopped is False


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


@pytest.fixture(autouse=True)
def _isolate_armed_snapshot(tmp_path, monkeypatch):
    """2026-10-16：start_strategy/stop_strategy 現在每次都會寫
    _persist_armed_snapshot() 的快照檔，同樣隔離到 tmp_path，避免測試假資料混進
    專案真實的 data/armed_strategies.json（這份是用來讓真正的服務重啟後自動恢復
    策略用的，被測試資料污染會導致下次真的啟動時恢復出測試裡的假策略）。"""
    monkeypatch.setattr(strategy_service, "_ARMED_SNAPSHOT_PATH", tmp_path / "armed_strategies.json")


@pytest.fixture(autouse=True)
def _pin_current_tmf_code(monkeypatch):
    """
    2026-09-17 實盤事故排查發現：這個檔案裡幾乎所有測試都寫死用 "TM2609"
    當「目前近月合約」的假設，但 compute_current_tmf_code() 是照真實系統
    時鐘算的——一旦 wall-clock 真的跨過當月結算日（每月第三個星期三），
    "TM2609" 就不再是「目前近月」，_tick_one() 的自動換月邏輯會在測試裡
    也被觸發，把這些測試的 product_code 偷偷改掉、連帶讓 last_signal_key
    被預先標記，導致一堆看似無關的測試莫名其妙壞掉（9/17 當天實測發生過）。
    這裡把「目前近月」釘死在 TM2609，讓測試結果不再看當下日期臉色。
    """
    monkeypatch.setattr(strategy_service, "compute_current_tmf_code", lambda: "TM2609")


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


def test_strategy_tick_isolates_one_strategys_crash_from_others_and_reconcile(clean_armed):
    """2026-10-07 加固：strategy_tick() 原本對每個策略呼叫 _tick_one 完全沒包
    try/except，任何一個策略內部丟出未預期例外（資料髒污、pandas 計算出錯等，
    不限於新加的缺口檢查），都會讓這一輪 strategy_tick() 直接中斷——後面排隊的
    其他策略這輪全部被跳過、連 reconcile_after_manual_close／
    reconcile_orphan_stop_orders 這種帳務核對都不會執行。這裡驗證：壞掉的
    策略被隔離在自己身上（其他策略照常跑完、reconcile 仍然執行），不會拖垮
    整輪。"""
    broken = _open_long_state(strategy_id="breakout_long")
    healthy = _open_long_state(strategy_id="breakout_short", held_qty=0, held_direction=None)
    _armed["breakout_long"] = broken
    _armed["breakout_short"] = healthy

    healthy_ticked = []

    def _fake_tick_one(state, svc, st):
        if state.strategy_id == "breakout_long":
            raise RuntimeError("模擬資料髒污導致的未預期例外")
        healthy_ticked.append(state.strategy_id)

    with patch.object(strategy_service.TradingService, "get") as mock_get, patch.object(
        strategy_service, "_tick_one", side_effect=_fake_tick_one
    ), patch.object(strategy_service, "reconcile_after_manual_close") as mock_reconcile, patch.object(
        strategy_service, "reconcile_orphan_stop_orders"
    ) as mock_reconcile_orphan:
        mock_get.return_value.status.return_value = {"connected": True, "positions": []}
        mock_reconcile.return_value = []
        strategy_service.strategy_tick()

    assert healthy_ticked == ["breakout_short"], "壞掉的策略不該讓排在它後面的健康策略被跳過"
    assert "執行異常" in broken.last_action
    mock_reconcile.assert_called_once()
    mock_reconcile_orphan.assert_called_once()


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

    with patch.object(strategy_service, "get_kline_store", return_value=fake_store):
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
    assert call_kwargs.get("long_action_flag") is True  # 實測驗證通過，正式開啟長效保護


def test_place_oco_protection_queries_and_stores_long_action_key_when_enabled():
    # 2026-10-07：長效單預設關閉，但功能要先驗證「打開之後真的會查、真的會存對」，
    # 用 monkeypatch 打開開關來測，不代表這是現在的正式行為。
    state = _open_long_state(product_code="TM2609", entry_price=20000.0)
    svc = MagicMock()
    svc.place_oco_order.return_value = _oco_success_response()
    svc.query_stop_loss_report.return_value = {
        "stop_loss_rows": [
            {"smart_key": "99999999", "long_action_key": "LA_WRONG"},  # 不同智慧單號，不該被取用
            {"smart_key": "26564233", "long_action_key": "LA000789"},
        ]
    }

    with patch.object(strategy_service, "USE_LONG_ACTION_PROTECTION", True):
        _place_oco_protection_for_state(state, svc)

    call_kwargs = svc.place_oco_order.call_args.args[0]
    assert call_kwargs["long_action_flag"] is True
    svc.query_stop_loss_report.assert_called_once()
    assert state.protection_order_long_action_key == "LA000789"


def test_place_oco_protection_long_action_key_stays_none_when_query_has_no_match():
    state = _open_long_state(product_code="TM2609", entry_price=20000.0)
    svc = MagicMock()
    svc.place_oco_order.return_value = _oco_success_response()
    svc.query_stop_loss_report.return_value = {"stop_loss_rows": [{"smart_key": "不一樣", "long_action_key": "LA1"}]}

    with patch.object(strategy_service, "USE_LONG_ACTION_PROTECTION", True):
        _place_oco_protection_for_state(state, svc)

    assert state.protection_order_long_action_key is None


def test_place_oco_protection_long_action_key_ignores_zero_value():
    # "0" 是非長效單的填法（見官方文件），就算查詢結果裡出現也不該被當成真的長效單號存下來。
    state = _open_long_state(product_code="TM2609", entry_price=20000.0)
    svc = MagicMock()
    svc.place_oco_order.return_value = _oco_success_response()
    svc.query_stop_loss_report.return_value = {"stop_loss_rows": [{"smart_key": "26564233", "long_action_key": "0"}]}

    with patch.object(strategy_service, "USE_LONG_ACTION_PROTECTION", True):
        _place_oco_protection_for_state(state, svc)

    assert state.protection_order_long_action_key is None


def test_place_oco_protection_survives_query_exception_without_losing_protection():
    # 查詢長效單號失敗，不該影響已經掛成功的 OCO——smart_key 還是要記住，
    # last_action 不該被誤導成「OCO 掛單失敗」（這是這次順手修掉的一個遺留 bug）。
    state = _open_long_state(product_code="TM2609", entry_price=20000.0)
    svc = MagicMock()
    svc.place_oco_order.return_value = _oco_success_response()
    svc.query_stop_loss_report.side_effect = RuntimeError("連線中斷")

    with patch.object(strategy_service, "USE_LONG_ACTION_PROTECTION", True):
        _place_oco_protection_for_state(state, svc)

    assert state.protection_order_smart_key == "26564233"
    assert state.protection_order_long_action_key is None
    assert "掛單失敗" not in state.last_action


def test_cancel_protection_order_passes_long_action_key_when_present():
    state = _open_long_state(product_code="TM2609", protection_order_smart_key="26564233")
    state.protection_order_long_action_key = "LA000789"
    svc = MagicMock()
    svc.cancel_stop_order.return_value = {"cancel_result": {"success": True}}

    _cancel_protection_order_for_state(state, svc)

    call_kwargs = svc.cancel_stop_order.call_args.args[0]
    assert call_kwargs["long_action_key"] == "LA000789"
    assert state.protection_order_long_action_key is None  # 撤單成功後清空


def test_cancel_protection_order_passes_empty_string_when_no_long_action_key():
    # 沒有長效單號（一般情況）時傳空字串，讓 trading_service 補預設值 "0"，
    # 不要自己在這裡猜一個值。
    state = _open_long_state(product_code="TM2609", protection_order_smart_key="26564233")
    svc = MagicMock()
    svc.cancel_stop_order.return_value = {"cancel_result": {"success": True}}

    _cancel_protection_order_for_state(state, svc)

    call_kwargs = svc.cancel_stop_order.call_args.args[0]
    assert call_kwargs["long_action_key"] == ""


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


def test_place_oco_protection_for_state_clamps_take_profit_past_reference_price():
    """2026-09-11 實盤事故：entry_price 因為報價過期記成 46082，但下單當下真實
    市價已經在 46390，算出來的停利觸發價（46082+250=46332）比真實市價還低，
    OCO 一掛到券商那邊就立刻被現價觸發、秒平倉。傳入 reference_price（可靠的
    最新K棒收盤價）之後，應該偵測到停利觸發價已經在現價的錯誤那一側（<=現價），
    改用「現價+停利點數」重算，確保掛出去的時候還沒被現價觸發。"""
    state = _open_long_state(product_code="TM2609", entry_price=46082.0)
    svc = MagicMock()
    svc.place_oco_order.return_value = _oco_success_response()

    _place_oco_protection_for_state(state, svc, reference_price=46390.0)

    call_kwargs = svc.place_oco_order.call_args.args[0]
    assert call_kwargs["trigger_price"] == "46640"  # 46390 + 250，不是 46082+250=46332
    assert call_kwargs["trigger_price2"] == "45982"  # 停損本來就在現價正確一側，不用校正


def test_place_oco_protection_for_state_no_clamp_when_reference_price_missing():
    """reference_price 沒傳（None）時，行為完全比照原本邏輯，不做任何校正——
    確保這個防呆是可選的加固，不影響既有呼叫端（例如舊測試）。"""
    state = _open_long_state(product_code="TM2609", entry_price=46082.0)
    svc = MagicMock()
    svc.place_oco_order.return_value = _oco_success_response()

    _place_oco_protection_for_state(state, svc, reference_price=None)

    call_kwargs = svc.place_oco_order.call_args.args[0]
    assert call_kwargs["trigger_price"] == "46332"
    assert call_kwargs["trigger_price2"] == "45982"


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
    # 有真正的 OCO 保護單頂著時，在安全緩衝內軟停損檢查要讓路，讓券商 OCO 優先執行。
    # 預設 stop_loss_points=100，跌 105 點（19895.0）在 10 點安全緩衝內（尚未穿價超過 110 點）。
    state = _open_long_state(product_code="TM2609", protection_order_smart_key="26564233")
    svc = MagicMock()
    st = {"quote": {"last_price": 19895.0}}

    _tick_one(state, svc, st)

    svc.place_order.assert_not_called()
    assert state.held_qty == 1  # 沒被軟停損平掉


def test_tick_one_emergency_stop_when_price_deeply_breaches_stop_loss_with_broker_order(clean_armed):
    # 2026-10-07 事故防護：即使記有券商 OCO 智慧單號，若現價已穿過停損超過安全緩衝（10 點），
    # 研判券商智慧單未觸發或已跨盤失效，本地必須啟動「緊急硬穿價軟停損」市價平倉！
    state = _open_long_state(product_code="TM2609", protection_order_smart_key="26564233")
    svc = MagicMock()
    svc.place_order.return_value = {"order_result": {"success": True}}
    svc.cancel_stop_order.return_value = {"cancel_result": {"success": True}}
    # 預設 stop_loss_points=100，跌 115 點（19885.0）超過 100 + 10 = 110 點安全緩衝門檻
    st = {"quote": {"last_price": 19885.0}}

    _tick_one(state, svc, st)

    svc.place_order.assert_called_once()
    assert state.held_qty == 0
    assert "緊急穿價停損" in state.stop_reason


def test_tick_one_cancels_protection_order_before_sending_close_order(clean_armed):
    # 2026-10-07 事故根因：2026-08-27 已經發生過一次同樣的 bug——分開送兩張平倉
    # 委託，第二張因為第一張還佔著平倉額度被 [999] 拒單。強制平倉時若 OCO 保護單
    # 還掛著，必須先撤單釋放額度，再送平倉單，順序顛倒會重蹈覆轍。
    state = _open_long_state(product_code="TM2609", protection_order_smart_key="26564233")
    svc = MagicMock()
    svc.place_order.return_value = {"order_result": {"success": True}}
    svc.cancel_stop_order.return_value = {"cancel_result": {"success": True}}
    st = {"quote": {"last_price": 19885.0}}  # 穿價超過安全緩衝，觸發緊急停損

    _tick_one(state, svc, st)

    svc.cancel_stop_order.assert_called_once()
    svc.place_order.assert_called_once()
    call_order = [call[0] for call in svc.mock_calls]
    assert call_order.index("cancel_stop_order") < call_order.index("place_order"), (
        "撤單必須先於平倉單送出，否則 OCO 還佔著平倉額度會讓平倉被 [999] 拒單"
    )


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
    ), patch.object(
        strategy_service, "_now", return_value=pd.Timestamp("2026-08-21T09:45:00") + pd.Timedelta(minutes=5)
    ), patch.object(strategy_service, "_load_bars", return_value=fake_bars), patch.object(
        strategy_service, "_signaled_frame", return_value=MagicMock()
    ):
        _tick_one(state, svc, st)

    assert state.held_qty == 1
    svc.place_oco_order.assert_called_once()
    payload = svc.place_oco_order.call_args[0][0]
    # 2026-10-07：長效單實測驗證通過，正式開啟長效保護（long_action_flag=True）。
    assert payload.get("long_action_flag") is True
    assert state.protection_order_smart_key == "26564233"


def test_tick_one_entry_skips_oco_when_disabled(clean_armed):
    state = _open_long_state(
        product_code="TM2609", held_qty=0, held_direction=None, entry_price=0.0,
        oco_enabled=False,
    )
    state.last_signal_key = None
    svc = MagicMock()
    svc.place_order.return_value = {"order_result": {"success": True}}
    st = {"quote": {"last_price": 20000.0}}

    fake_bars = MagicMock()
    fake_bars.empty = False
    fake_bars.__len__.return_value = 100

    with patch.object(
        strategy_service,
        "_latest_closed_signal",
        return_value={"time": "2026-08-21T09:45:00", "direction": "long", "key": "k1"},
    ), patch.object(
        strategy_service, "_now", return_value=pd.Timestamp("2026-08-21T09:45:00") + pd.Timedelta(minutes=5)
    ), patch.object(strategy_service, "_load_bars", return_value=fake_bars), patch.object(
        strategy_service, "_signaled_frame", return_value=MagicMock()
    ):
        _tick_one(state, svc, st)

    assert state.held_qty == 1
    svc.place_oco_order.assert_not_called()
    assert state.protection_order_smart_key is None


def test_tick_one_reverse_signal_closes_position_by_default(clean_armed):
    """
    reverse_signal_exit_enabled 預設 True，方向限定策略遇到反向訊號、有留倉
    時應該平倉出場（見 test_tick_one_reverse_signal_skipped_when_disabled
    驗證關掉之後的行為）。
    """
    state = _open_long_state(product_code="TM2609")  # direction_limit="long", held_qty=1
    svc = MagicMock()
    svc.place_order.return_value = {"order_result": {"success": True}}
    st = {"quote": {"last_price": 20050.0}}

    fake_bars = MagicMock()
    fake_bars.empty = False
    fake_bars.__len__.return_value = 100

    with patch.object(
        strategy_service,
        "_latest_closed_signal",
        return_value={"time": "2026-08-21T09:45:00", "direction": "short", "key": "k1"},
    ), patch.object(
        strategy_service, "_now", return_value=pd.Timestamp("2026-08-21T09:45:00") + pd.Timedelta(minutes=5)
    ), patch.object(strategy_service, "_load_bars", return_value=fake_bars), patch.object(
        strategy_service, "_signaled_frame", return_value=MagicMock()
    ):
        _tick_one(state, svc, st)

    svc.place_order.assert_called_once()
    assert state.held_qty == 0


def test_tick_one_reverse_signal_skipped_when_disabled(clean_armed):
    state = _open_long_state(product_code="TM2609", reverse_signal_exit_enabled=False)
    svc = MagicMock()
    st = {"quote": {"last_price": 20050.0}}

    fake_bars = MagicMock()
    fake_bars.empty = False
    fake_bars.__len__.return_value = 100

    with patch.object(
        strategy_service,
        "_latest_closed_signal",
        return_value={"time": "2026-08-21T09:45:00", "direction": "short", "key": "k1"},
    ), patch.object(
        strategy_service, "_now", return_value=pd.Timestamp("2026-08-21T09:45:00") + pd.Timedelta(minutes=5)
    ), patch.object(strategy_service, "_load_bars", return_value=fake_bars), patch.object(
        strategy_service, "_signaled_frame", return_value=MagicMock()
    ):
        _tick_one(state, svc, st)

    svc.place_order.assert_not_called()
    assert state.held_qty == 1
    assert "已關閉反向訊號出場" in state.last_action


def test_tick_one_reverse_signal_flat_position_unaffected_by_toggle(clean_armed):
    # 空手時遇到反向訊號，不管開關是 True 還是 False，訊息都該是「空手不
    # 動作」，不能被誤導成「已關閉反向出場」（code review 抓到的巢狀結構
    # 要求：開關判斷要放在 held_qty > 0 分支內側，不是最外層）。
    state = _open_long_state(
        product_code="TM2609", held_qty=0, held_direction=None, entry_price=0.0,
        reverse_signal_exit_enabled=False,
    )
    state.last_signal_key = None
    svc = MagicMock()
    st = {"quote": {"last_price": 20050.0}}

    fake_bars = MagicMock()
    fake_bars.empty = False
    fake_bars.__len__.return_value = 100

    with patch.object(
        strategy_service,
        "_latest_closed_signal",
        return_value={"time": "2026-08-21T09:45:00", "direction": "short", "key": "k1"},
    ), patch.object(
        strategy_service, "_now", return_value=pd.Timestamp("2026-08-21T09:45:00") + pd.Timedelta(minutes=5)
    ), patch.object(strategy_service, "_load_bars", return_value=fake_bars), patch.object(
        strategy_service, "_signaled_frame", return_value=MagicMock()
    ):
        _tick_one(state, svc, st)

    svc.place_order.assert_not_called()
    assert state.last_action == "空手，反向訊號不動作（本策略不做這個方向）"


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
    ), patch.object(
        strategy_service, "_now", return_value=pd.Timestamp("2026-08-21T09:45:00") + pd.Timedelta(minutes=5)
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
        ), patch.object(
            strategy_service, "_now", return_value=pd.Timestamp("2026-08-25T09:45:00") + pd.Timedelta(minutes=5)
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


def test_start_strategy_accepts_custom_risk_parameters(clean_armed):
    """驗證 start_strategy 能正確套用依帳號/策略客製化的風控參數。"""
    from backend.strategy_service import start_strategy, _armed

    with patch("backend.strategy_service._load_bars", return_value=pd.DataFrame()):
        res = start_strategy(
            "breakout_long",
            "TM2609",
            qty=2,
            stop_loss_points=80.0,
            take_profit_points=200.0,
            max_loss_ntd=5000.0,
            max_loss_pct=0.05,
        )

    state = _armed["breakout_long"]
    assert state.qty == 2
    assert state.stop_loss_points == 80.0
    assert state.take_profit_points == 200.0
    assert state.max_loss_ntd == 5000.0
    assert state.max_loss_pct == 0.05


def test_start_strategy_applies_layer_toggle_overrides(clean_armed):
    from backend.strategy_service import start_strategy, _armed

    with patch("backend.strategy_service._load_bars", return_value=pd.DataFrame()):
        start_strategy(
            "breakout_long", "TM2609",
            oco_enabled=False, soft_stop_enabled=False,
            risk_insurance_enabled=True, reverse_signal_exit_enabled=False,
        )

    state = _armed["breakout_long"]
    assert state.oco_enabled is False
    assert state.soft_stop_enabled is False
    assert state.risk_insurance_enabled is True
    assert state.reverse_signal_exit_enabled is False


def test_start_strategy_defaults_all_toggles_true_when_omitted(clean_armed):
    from backend.strategy_service import start_strategy, _armed

    with patch("backend.strategy_service._load_bars", return_value=pd.DataFrame()):
        start_strategy("breakout_long", "TM2609")

    state = _armed["breakout_long"]
    assert state.oco_enabled is True
    assert state.soft_stop_enabled is True
    assert state.risk_insurance_enabled is True
    assert state.reverse_signal_exit_enabled is True


def test_resolve_strategy_product_code():
    from backend.strategy_service import resolve_strategy_product_code

    current_tmf = strategy_service.compute_current_tmf_code()
    assert resolve_strategy_product_code(None) == current_tmf
    assert resolve_strategy_product_code("") == current_tmf
    assert resolve_strategy_product_code("TMF") == current_tmf
    assert resolve_strategy_product_code("TM") == current_tmf
    assert resolve_strategy_product_code("MTX") == "MTX"
    assert resolve_strategy_product_code("TX") == "TX"
    assert resolve_strategy_product_code("TM2610") == "TM2610"


def test_start_strategy_with_none_or_empty_product_code_resolves_to_front_month(clean_armed):
    from backend.strategy_service import start_strategy, _armed

    current_tmf = strategy_service.compute_current_tmf_code()
    with patch("backend.strategy_service._load_bars", return_value=pd.DataFrame()):
        start_strategy("breakout_long", product_code=None)

    state = _armed["breakout_long"]
    assert state.product_code == current_tmf


def test_resolve_strategy_product_code_corrects_expired_tmf():
    from backend.strategy_service import resolve_strategy_product_code

    current_tmf = strategy_service.compute_current_tmf_code()
    # 傳入過去已過期的 TM2608，應自動校正為當前近月
    assert resolve_strategy_product_code("TM2608") == current_tmf


# 2026-10-16 加固：單帳號桌面部署用本機檔案記住「目前開著的策略」，backend process
# 重啟後（不管是VM重開機、或任何其他原因）能自動恢復成重啟前的樣子，不用使用者
# 每次重啟都手動回儀表板按啟動。
def test_start_strategy_writes_armed_snapshot_to_disk(clean_armed):
    from backend.strategy_service import start_strategy

    with patch("backend.strategy_service._load_bars", return_value=pd.DataFrame()):
        start_strategy("breakout_long", "TM2609", qty=3, stop_loss_points=80.0)

    rows = json.loads(strategy_service._ARMED_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert len(rows) == 1
    assert rows[0]["strategy_id"] == "breakout_long"
    assert rows[0]["product_code"] == "TM2609"
    assert rows[0]["qty"] == 3
    assert rows[0]["stop_loss_points"] == 80.0


def test_stop_strategy_without_position_removes_it_from_snapshot(clean_armed):
    from backend.strategy_service import start_strategy, stop_strategy

    with patch("backend.strategy_service._load_bars", return_value=pd.DataFrame()):
        start_strategy("breakout_long", "TM2609")
    stop_strategy("breakout_long")

    rows = json.loads(strategy_service._ARMED_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert rows == []


def test_stop_strategy_with_open_position_still_removes_it_from_snapshot(clean_armed):
    """即使還有留倉在等平倉（stop_strategy 不會真的移除 _armed，只標記
    stopped=True，見該函式註解），快照裡也不該留著它——使用者已經明確按過停止，
    重啟後不該又被自動恢復成「開著」。"""
    state = _open_long_state(strategy_id="breakout_long")  # held_qty=1
    strategy_service._armed["breakout_long"] = state

    stop_strategy("breakout_long")

    assert strategy_service._armed["breakout_long"].stopped is True  # 還在監控平倉
    rows = json.loads(strategy_service._ARMED_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert rows == []


def test_resume_armed_strategies_from_snapshot_rearms_saved_config(clean_armed):
    from backend.strategy_service import resume_armed_strategies_from_snapshot

    strategy_service._ARMED_SNAPSHOT_PATH.write_text(
        json.dumps([{
            "strategy_id": "breakout_long", "product_code": "TM2609",
            "qty": 5, "stop_loss_points": 60.0, "take_profit_points": 150.0,
            "max_loss_ntd": 8000.0, "max_loss_pct": 0.2,
            "oco_enabled": False, "soft_stop_enabled": True,
            "risk_insurance_enabled": False, "reverse_signal_exit_enabled": True,
        }]),
        encoding="utf-8",
    )

    with patch("backend.strategy_service._load_bars", return_value=pd.DataFrame()):
        resumed = resume_armed_strategies_from_snapshot()

    assert resumed == 1
    state = strategy_service._armed["breakout_long"]
    assert state.qty == 5
    assert state.stop_loss_points == 60.0
    assert state.oco_enabled is False
    assert state.risk_insurance_enabled is False


def test_resume_armed_strategies_from_snapshot_noop_when_no_file(clean_armed):
    from backend.strategy_service import resume_armed_strategies_from_snapshot

    assert not strategy_service._ARMED_SNAPSHOT_PATH.exists()
    assert resume_armed_strategies_from_snapshot() == 0
    assert strategy_service._armed == {}


def test_resume_armed_strategies_from_snapshot_respects_opt_out_env_var(clean_armed, monkeypatch):
    from backend.strategy_service import resume_armed_strategies_from_snapshot

    strategy_service._ARMED_SNAPSHOT_PATH.write_text(
        json.dumps([{"strategy_id": "breakout_long", "product_code": "TM2609"}]), encoding="utf-8"
    )
    monkeypatch.setenv("AUTO_RESUME_STRATEGIES", "0")

    assert resume_armed_strategies_from_snapshot() == 0
    assert strategy_service._armed == {}


# 2026-10-16 加固（Gemini review 抓到的真實漏洞，讀過 reconcile_after_manual_close/
# reconcile_orphan_stop_orders 的程式碼驗證過）：兩個既有的 reconcile 函式都只處理
# 「內部有、券商沒有」，完全沒有反過來比對「內部沒有、券商有」——resume 恢復的策略
# 一律從 held_qty=0 開始，如果重啟前那一刻券商端其實還握著真倉位，放著不管會讓策略
# 誤判空手、對同一方向再送一次進場單，軟停損也不會替那筆舊倉位計算損益。
def test_resume_pauses_strategy_when_broker_has_unclaimed_position(clean_armed):
    from backend.strategy_service import resume_armed_strategies_from_snapshot

    strategy_service._ARMED_SNAPSHOT_PATH.write_text(
        json.dumps([{"strategy_id": "breakout_long", "product_code": "TM2609"}]), encoding="utf-8"
    )

    with patch("backend.strategy_service._load_bars", return_value=pd.DataFrame()), patch.object(
        strategy_service.TradingService, "get"
    ) as mock_get:
        # breakout_long 的 direction_limit 是 "long"，券商端剛好回報同商品同方向
        # 有 1 口留倉，但快照裡完全不知道這件事（本機重啟前的狀態）。
        mock_get.return_value.status.return_value = {
            "positions": [{"product": "TM2609", "direction_key": "long", "qty": "1"}]
        }
        resumed = resume_armed_strategies_from_snapshot()

    assert resumed == 1  # 有被重新 arm 起來，只是接著被暫停，不是沒恢復
    state = strategy_service._armed["breakout_long"]
    assert state.stopped is True
    assert "未被認領的留倉" in state.stop_reason
    # 暫停之後要重新寫回快照，不然下次重啟又會用同一份舊設定重新恢復、重演一次
    rows = json.loads(strategy_service._ARMED_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert rows == []


def test_resume_does_not_pause_when_broker_position_matches_nothing(clean_armed):
    """對照組：券商沒有留倉、或留倉是別的商品/方向，不該被誤判成孤兒倉位而暫停。"""
    from backend.strategy_service import resume_armed_strategies_from_snapshot

    strategy_service._ARMED_SNAPSHOT_PATH.write_text(
        json.dumps([{"strategy_id": "breakout_long", "product_code": "TM2609"}]), encoding="utf-8"
    )

    with patch("backend.strategy_service._load_bars", return_value=pd.DataFrame()), patch.object(
        strategy_service.TradingService, "get"
    ) as mock_get:
        mock_get.return_value.status.return_value = {"positions": []}
        resumed = resume_armed_strategies_from_snapshot()

    assert resumed == 1
    state = strategy_service._armed["breakout_long"]
    assert state.stopped is False


def test_auto_roll_premarks_existing_signal_and_does_not_fire_stale_order():
    """驗證：當 _tick_one 觸發 auto-roll 換約時，必須立即將新商品的既有訊號標記為已消費，
    絕不能對著換約前就已存在的歷史 MA 狀態誤發真實市價委託！"""
    from backend.strategy_service import StrategyState, _tick_one
    from unittest.mock import MagicMock, patch

    state = StrategyState(
        strategy_id="breakout_long",
        product_code="TM2608",
        strategy="breakout",
        direction_limit="long",
        label="測試",
        qty=1,
        initial_capital_ntd=100000.0,
        max_loss_ntd=10000.0,
        max_loss_pct=0.1,
        stop_loss_points=100.0,
        take_profit_points=250.0,
    )
    assert state.last_signal_key is None

    mock_svc = MagicMock()
    mock_svc.is_order_enabled.return_value = True

    dummy_signal = {"key": "2026-09-01 10:00:00_long", "direction": "long", "close": 46800.0, "time": 1788200000}
    mock_df = pd.DataFrame({"close": [46800.0] * 70, "ma_fast": [46800.0] * 70, "ma_mid": [46700.0] * 70, "ma_slow": [46600.0] * 70})

    with patch("backend.strategy_service._latest_closed_signal", return_value=dummy_signal), \
         patch("backend.strategy_service._load_bars", return_value=mock_df):
        st = {"quote": {"last_price": 46800.0}}
        _tick_one(state, mock_svc, st)

    # 換約完成且歷史訊號已被預先消費
    assert state.product_code == "TM2609"
    assert state.last_signal_key == "2026-09-01 10:00:00_long"
    assert not mock_svc.place_order.called, "安全防護失效：換約瞬間不可對著舊訊號下單！"


def test_opposite_direction_position_exists_detects_sibling_holding(clean_armed):
    """驗證：同商品、方向相反、目前持倉中的另一支策略，會被互斥防護偵測到。"""
    holder = StrategyState(
        strategy_id="breakout_short",
        product_code="TM2609",
        strategy="breakout",
        direction_limit="short",
        label="測試空",
        held_qty=1,
        held_direction="short",
    )
    _armed["breakout_short"] = holder

    assert _opposite_direction_position_exists(
        "TM2609", "long", exclude_strategy_id="breakout_long"
    )
    # 同方向（都是 short）不算「反向」，不應該被擋
    assert not _opposite_direction_position_exists(
        "TM2609", "short", exclude_strategy_id="breakout_short"
    )
    # 不同商品不算衝突
    assert not _opposite_direction_position_exists(
        "MTX", "long", exclude_strategy_id="breakout_long"
    )
    # 空手的話不算「持倉中」，不應該被擋
    holder.held_qty = 0
    assert not _opposite_direction_position_exists(
        "TM2609", "long", exclude_strategy_id="breakout_long"
    )


def test_tick_one_skips_entry_when_opposite_direction_sibling_holds_position(clean_armed):
    """驗證：breakout_short 抱著空單時，breakout_long 看到多頭訊號應該暫緩進場，
    不送出會被券商淨沖銷掉的買進新倉單（2026-09-08 實盤事故：這個沒擋住，
    導致空單被買單沖銷、緊接著多單自己的 OCO 保護單又因「留倉部位不足」被拒單，
    這筆倉位因此完全失去保護）。"""
    holder = StrategyState(
        strategy_id="breakout_short",
        product_code="TM2609",
        strategy="breakout",
        direction_limit="short",
        label="測試空",
        held_qty=1,
        held_direction="short",
    )
    _armed["breakout_short"] = holder

    state = StrategyState(
        strategy_id="breakout_long",
        product_code="TM2609",
        strategy="breakout",
        direction_limit="long",
        label="測試多",
        qty=1,
        initial_capital_ntd=100000.0,
        max_loss_ntd=10000.0,
        max_loss_pct=0.1,
        stop_loss_points=100.0,
        take_profit_points=250.0,
    )
    _armed["breakout_long"] = state

    mock_svc = MagicMock()
    mock_svc.is_order_enabled.return_value = True

    dummy_signal = {"key": "2026-09-08 01:00:00_long", "direction": "long", "close": 47500.0, "time": 1788213600}
    mock_df = pd.DataFrame({"close": [47500.0] * 70, "ma_fast": [47500.0] * 70, "ma_mid": [47400.0] * 70, "ma_slow": [47300.0] * 70})
    # dummy_signal 的 "time" 是整數（單位是秒的 unix epoch），但 _tick_one 裡用
    # pd.Timestamp(signal["time"]) 轉換時沒有指定 unit——這是 production code 的既有
    # 行為（真正的 _latest_closed_signal 永遠回傳 ISO 字串，不會走到這個整數分支），
    # 這裡 _now 刻意比照同一種（無 unit）解讀方式，避免測試誤判「訊號過期」。
    fake_now = pd.Timestamp(dummy_signal["time"]) + pd.Timedelta(minutes=5)

    with patch("backend.strategy_service._latest_closed_signal", return_value=dummy_signal), \
         patch("backend.strategy_service._now", return_value=fake_now), \
         patch("backend.strategy_service._load_bars", return_value=mock_df):
        st = {"quote": {"last_price": 47500.0}}
        _tick_one(state, mock_svc, st)

    assert not mock_svc.place_order.called, "互斥防護失效：不該對著反向持倉中的商品送出新倉單！"
    assert state.held_qty == 0
    # 沒有消費這個訊號的 key——下一輪還要繼續重試，等空單真正出場後才放行
    assert state.last_signal_key is None
    assert "暫緩進場" in state.last_action

    # 空單真正出場（held_qty 歸零）之後，同一個訊號應該就能順利進場了
    holder.held_qty = 0
    holder.held_direction = None
    with patch("backend.strategy_service._latest_closed_signal", return_value=dummy_signal), \
         patch("backend.strategy_service._now", return_value=fake_now), \
         patch("backend.strategy_service._load_bars", return_value=mock_df):
        _tick_one(state, mock_svc, st)

    assert mock_svc.place_order.called, "反向持倉出場後，應該要能正常進場"


# 2026-10-16 加固（同一次 Gemini review 抓到的第二個漏洞）：孤兒倉位暫停機制的
# 第一版只擋「跟孤兒倉位同方向」的策略，漏了跨方向沖銷死角——孤兒倉位如果是空單，
# 被暫停的 breakout_short held_qty 還是 0，breakout_long 看到多頭訊號時
# _opposite_direction_position_exists 查的是「其他策略的 held_qty」，完全查不到
# 已經被暫停、held_qty=0 的孤兒策略，一樣會誤判「沒有反向持倉」而放行，買單一送
# 出就會被券商拿去跟孤兒空單直接淨額沖銷（2026-09-08 那次實盤事故重演）。
def test_unclaimed_broker_position_for_product_detects_opposite_direction_orphan():
    from backend.strategy_service import _unclaimed_broker_position_for_product

    # breakout_short 被暫停了，held_qty=0，完全沒有策略認領這口券商端的空單。
    holder = StrategyState(
        strategy_id="breakout_short", product_code="TM2609", strategy="breakout",
        direction_limit="short", label="測試空", held_qty=0, held_direction=None, stopped=True,
    )
    strategy_service._armed["breakout_short"] = holder
    broker_positions = [{"product": "TM2609", "direction_key": "short", "qty": "1"}]

    assert _unclaimed_broker_position_for_product("TM2609", broker_positions) == 1


def test_unclaimed_broker_position_for_product_zero_when_fully_claimed():
    from backend.strategy_service import _unclaimed_broker_position_for_product

    holder = StrategyState(
        strategy_id="breakout_short", product_code="TM2609", strategy="breakout",
        direction_limit="short", label="測試空", held_qty=1, held_direction="short",
    )
    strategy_service._armed["breakout_short"] = holder
    broker_positions = [{"product": "TM2609", "direction_key": "short", "qty": "1"}]

    assert _unclaimed_broker_position_for_product("TM2609", broker_positions) == 0


def test_tick_one_blocks_entry_on_unclaimed_opposite_direction_orphan(clean_armed):
    """核心回歸測試：重演上面漏洞的完整情境——breakout_short 被暫停、held_qty=0，
    券商端其實還握著那口空單。breakout_long 看到多頭訊號時，即使
    _opposite_direction_position_exists 會誤判沒有反向持倉，新加的
    _unclaimed_broker_position_for_product 守門員也要擋下來，不能送出買單。"""
    holder = StrategyState(
        strategy_id="breakout_short", product_code="TM2609", strategy="breakout",
        direction_limit="short", label="測試空", held_qty=0, held_direction=None, stopped=True,
    )
    _armed["breakout_short"] = holder

    state = StrategyState(
        strategy_id="breakout_long", product_code="TM2609", strategy="breakout",
        direction_limit="long", label="測試多", qty=1,
        initial_capital_ntd=100000.0, max_loss_ntd=10000.0, max_loss_pct=0.1,
        stop_loss_points=100.0, take_profit_points=250.0,
    )
    _armed["breakout_long"] = state

    mock_svc = MagicMock()
    mock_svc.is_order_enabled.return_value = True

    dummy_signal = {"key": "2026-09-08 01:00:00_long", "direction": "long", "close": 47500.0, "time": 1788213600}
    mock_df = pd.DataFrame({"close": [47500.0] * 70, "ma_fast": [47500.0] * 70, "ma_mid": [47400.0] * 70, "ma_slow": [47300.0] * 70})
    fake_now = pd.Timestamp(dummy_signal["time"]) + pd.Timedelta(minutes=5)
    # 券商端其實還握著那口孤兒空單——這是漏洞的關鍵：舊的互斥檢查完全看不到這個。
    st = {"quote": {"last_price": 47500.0}, "positions": [{"product": "TM2609", "direction_key": "short", "qty": "1"}]}

    with patch("backend.strategy_service._latest_closed_signal", return_value=dummy_signal), \
         patch("backend.strategy_service._now", return_value=fake_now), \
         patch("backend.strategy_service._load_bars", return_value=mock_df):
        _tick_one(state, mock_svc, st)

    assert not mock_svc.place_order.called, "跨方向孤兒倉位沒擋住，買單會被券商拿去跟孤兒空單淨額沖銷！"
    assert state.stopped is True
    assert "未被認領的留倉" in state.stop_reason


def test_tick_one_discards_stale_quote_and_uses_bar_close_for_entry_price(clean_armed):
    """2026-09-11 實盤事故重現：st["quote"]["last_price"] 是跨盤沒更新的過期值
    （46082，實際是前一個夜盤 05:00 的收盤價），真實市價（也就是K棒收盤價）
    早就在 46390。舊邏輯無條件信任 quote>0，直接拿 46082 當進場價去算 OCO，
    停利觸發價因此已經在現價後面，一掛上去就秒觸發平倉。這裡驗證：報價跟
    K棒收盤價偏離超過 QUOTE_STALENESS_POINTS 時，entry_price 應該改用可靠的
    K棒收盤價（46390），不是那個過期的 46082。"""
    state = StrategyState(
        strategy_id="breakout_long",
        product_code="TM2609",
        strategy="breakout",
        direction_limit="long",
        label="測試多",
        qty=1,
        initial_capital_ntd=100000.0,
        max_loss_ntd=10000.0,
        max_loss_pct=0.1,
        stop_loss_points=100.0,
        take_profit_points=250.0,
    )
    _armed["breakout_long"] = state

    mock_svc = MagicMock()
    mock_svc.is_order_enabled.return_value = True
    # 沒有帶 "state"/positions，模擬下單成功但刷新快照裡還沒查得到最新持倉，
    # 逼進場價判定走到第 3 層備援（K棒收盤價）。
    mock_svc.place_order.return_value = {"order_result": {"success": True}}

    dummy_signal = {"key": "2026-09-11 18:00:00_long", "direction": "long", "close": 46390.0, "time": 1789120800}
    mock_df = pd.DataFrame({"close": [46390.0] * 70, "ma_fast": [46390.0] * 70, "ma_mid": [46300.0] * 70, "ma_slow": [46200.0] * 70})
    fake_now = pd.Timestamp(dummy_signal["time"]) + pd.Timedelta(minutes=5)

    with patch("backend.strategy_service._latest_closed_signal", return_value=dummy_signal), \
         patch("backend.strategy_service._now", return_value=fake_now), \
         patch("backend.strategy_service._load_bars", return_value=mock_df):
        st = {"quote": {"last_price": 46082.0}}  # 過期的舊報價，偏離現價 308 點
        _tick_one(state, mock_svc, st)

    assert mock_svc.place_order.called
    assert state.held_qty == 1
    assert state.entry_price == 46390.0, "應該改用K棒收盤價，不是那個過期308點的舊報價 46082"


def test_full_session_switch_simulation_end_to_end(clean_armed):
    """模擬完整的「日夜盤切換」情境，從連線層一路走到策略層，驗證兩層防護
    合起來真的能處理這個情境，不是只有各自獨立的單元測試：

    1. 連線層：模擬夜盤 05:00 收盤後報價卡住（跟真實事故一樣是 46082），
       接著模擬真實 log 觀察到的反覆斷線/重連序列（3001/3002/3001...），
       每次都應該清空過期報價、標記需要重新訂閱。
    2. 連線層：模擬 COM 專用執行緒 idle loop 呼叫 check_and_resubscribe_quotes()
       （不是直接在 OnConnection 事件裡呼叫），驗證會重新訂閱、並且模擬
       真正收到新報價後 quote 正確更新為當下真實市價 46390。
    3. 策略層：把這個連線層跑完之後的真實狀態餵給 _tick_one，驗證多單
       用的是重新訂閱後的新報價（46390），不是舊的 46082——證明兩層防護
       接起來後，整個系統在日夜盤切換情境下的最終行為是正確的。
    """
    broker = CapitalFuturesBroker()
    broker.live.connected = True
    broker.live.subscribed_product = "TM2609"
    broker._stocks_ready = True
    broker.live.quote.last_price = 46082.0  # 夜盤 05:00 收盤的舊報價
    broker.live.quote.updated_at = "2026-09-11 05:00:00"

    # 在觸發 OnConnection 前掛上 mock，嚴格驗證 OnConnection 執行期間絕不呼叫 RequestStocks / RequestTicks
    broker.request_stocks = MagicMock(return_value=0)
    broker.request_ticks = MagicMock(return_value=0)

    # --- 第 1 步：模擬真實 log 觀察到的重連與跨盤序列（3001/3002/3001/3003）---
    for n_kind in (3001, 3002, 3001, 3003):
        broker._quote_events_sink.OnConnection(n_kind, 0)
        assert broker.live.quote.last_price is None, "每次連線事件後，過期報價都應該被清空"

    assert broker._stocks_ready is True, "OnConnection(3003) 應自然將 _stocks_ready 設為 True，不需手動覆寫"
    assert broker._needs_resubscribe is True, "跨盤後應標記需要重新訂閱"
    # 嚴格斷言：OnConnection 回呼期間絕不能直接呼叫 RequestStocks（避免 COM 重入死鎖）
    broker.request_stocks.assert_not_called()
    broker.request_ticks.assert_not_called()

    # --- 第 2 步：模擬 COM 專用執行緒 idle loop（不是 OnConnection 事件內）呼叫重新訂閱 ---
    def _fake_snapshot(product_code: str) -> None:
        # 模擬真正重新訂閱之後，收到日盤開盤的真實成交價
        broker.live.quote.last_price = 46390.0
        broker.live.quote.updated_at = "2026-09-11 18:01:00"

    broker._snapshot_quote = MagicMock(side_effect=_fake_snapshot)

    resubscribed = broker.check_and_resubscribe_quotes()

    assert resubscribed is True
    assert broker._needs_resubscribe is False
    broker.request_stocks.assert_called_once_with("TM2609", page=1)
    broker.request_ticks.assert_called_once_with("TM2609", page=1)
    assert broker.live.quote.last_price == 46390.0, "重新訂閱後應該拿到當下真實市價，不是還停在舊值"

    # --- 第 3 步：把連線層跑完之後的真實狀態餵給策略層，驗證多單用的是新報價 ---
    st = broker.get_live_state()
    assert st["quote"]["last_price"] == 46390.0

    state = StrategyState(
        strategy_id="breakout_long",
        product_code="TM2609",
        strategy="breakout",
        direction_limit="long",
        label="測試多",
        qty=1,
        initial_capital_ntd=100000.0,
        max_loss_ntd=10000.0,
        max_loss_pct=0.1,
        stop_loss_points=100.0,
        take_profit_points=250.0,
    )
    _armed["breakout_long"] = state

    mock_svc = MagicMock()
    mock_svc.is_order_enabled.return_value = True
    mock_svc.place_order.return_value = {"order_result": {"success": True}}
    mock_svc.place_oco_order.return_value = {
        "order_result": {"success": True, "raw": "20260911,二擇一委託已送出 條件單號：99999999,x0000,99999999,1688100009999"}
    }

    dummy_signal = {"key": "2026-09-11 18:00:00_long", "direction": "long", "close": 46390.0, "time": 1789120800}
    mock_df = pd.DataFrame({"close": [46390.0] * 70, "ma_fast": [46390.0] * 70, "ma_mid": [46300.0] * 70, "ma_slow": [46200.0] * 70})
    fake_now = pd.Timestamp(dummy_signal["time"]) + pd.Timedelta(minutes=5)

    with patch("backend.strategy_service._latest_closed_signal", return_value=dummy_signal), \
         patch("backend.strategy_service._now", return_value=fake_now), \
         patch("backend.strategy_service._load_bars", return_value=mock_df):
        _tick_one(state, mock_svc, st)

    assert mock_svc.place_order.called
    assert state.entry_price == 46390.0, "連線層修好之後，策略層應該直接拿到正確的新報價，不需要靠K棒備援兜底"

    # OCO 停利觸發價應該是 46390+250=46640，不是舊報價算出來的 46082+250=46332
    oco_kwargs = mock_svc.place_oco_order.call_args.args[0]
    assert oco_kwargs["trigger_price"] == "46640"


def test_strategy_config_defaults_death_cross_uses_130_points_and_no_reverse_exit():
    defaults = strategy_config_defaults("death_cross_short")
    assert defaults["stop_loss_points"] == pytest.approx(130.0)
    assert defaults["take_profit_points"] == pytest.approx(130.0)
    assert defaults["reverse_signal_exit_enabled"] is False
    assert defaults["strategy"] == "death_cross"
    assert defaults["direction_limit"] == "short"


def test_strategy_config_defaults_breakout_unaffected_by_death_cross_branch():
    defaults = strategy_config_defaults("breakout_long")
    assert defaults["stop_loss_points"] == pytest.approx(100.0)
    assert defaults["take_profit_points"] == pytest.approx(250.0)
    assert defaults["reverse_signal_exit_enabled"] is True


def test_load_bars_death_cross_uses_15min_store_and_skips_csv_merge(monkeypatch):
    """death_cross 傳 interval_minutes=15 時：(1) 讀的是 15 分鐘 store 不是 60 分鐘；
    (2) 即時K棒不足時直接回傳現況，不觸發 60 分鐘 CSV 合併分支。"""
    calls = []

    def _fake_get_kline_store(product_code, interval_minutes=60):
        calls.append(interval_minutes)
        store = MagicMock()
        store.get_klines.return_value = []  # 模擬即時資料完全不足
        return store

    monkeypatch.setattr(strategy_service, "get_kline_store", _fake_get_kline_store)
    bars = _load_bars("TMF", product_code="TM2609", interval_minutes=15, min_bars_required=22)

    assert calls == [15]
    assert bars.empty


def test_load_bars_15min_skips_csv_merge_even_without_product_code(monkeypatch):
    """2026-09-30 最終審查抓到：CSV 合併分支的跳過判斷原本巢狀在 `if product_code`
    裡面，只有「有傳 product_code」時才會生效——如果哪天有呼叫端漏傳
    product_code，會直接漏接這個保護、意外落入 60 分鐘 CSV 合併分支，把不同
    時間軸的K棒混在一起。這個判斷式已經搬到 `if product_code` 區塊外面，不管
    有沒有 product_code 都要生效。用 spy 監看 Path.is_file 有沒有被拿去檢查
    60分鐘 CSV 候選路徑，證明真的完全沒進到那個分支，不是「剛好結果也是空的」
    這種弱驗證。"""
    from pathlib import Path

    checked_paths: list[str] = []
    original_is_file = Path.is_file

    def _spy_is_file(self):
        checked_paths.append(str(self))
        return original_is_file(self)

    monkeypatch.setattr(Path, "is_file", _spy_is_file)

    bars = _load_bars("TMF", product_code=None, interval_minutes=15, min_bars_required=22)

    assert bars.empty
    assert not any("60min" in p for p in checked_paths)


def test_load_bars_default_60min_behavior_unchanged(monkeypatch):
    """不傳 interval_minutes/min_bars_required 時，行為必須跟修改前完全一樣：讀 60 分鐘 store。"""
    calls = []

    def _fake_get_kline_store(product_code, interval_minutes=60):
        calls.append(interval_minutes)
        store = MagicMock()
        store.get_klines.return_value = []
        return store

    monkeypatch.setattr(strategy_service, "get_kline_store", _fake_get_kline_store)
    _load_bars("TMF", product_code="TM2609")

    assert calls == [60]


# 2026-10-06 事故（見 backend/strategy_service.py 裡 _has_abnormal_intraday_gap
# 上方的事故說明）：K 棒快取補齊前有缺口時，.shift(1) 比到的「上一根」其實是缺口
# 前的舊資料，算出來的交叉是假訊號。以下驗證缺口偵測本身，以及 _tick_one 真的會
# 因為偵測到缺口而不下單。
def test_bar_session_key_groups_day_session_together():
    assert strategy_service._bar_session_key(
        pd.Timestamp("2026-10-06 09:45")
    ) == strategy_service._bar_session_key(pd.Timestamp("2026-10-06 13:45"))


def test_bar_session_key_treats_midnight_hours_as_previous_nights_session():
    # 00:00~05:00 算進「前一天晚上開始」的夜盤，跟同一晚 23:00 算同一盤。
    assert strategy_service._bar_session_key(
        pd.Timestamp("2026-10-07 02:00")
    ) == strategy_service._bar_session_key(pd.Timestamp("2026-10-06 23:00"))


def test_has_abnormal_intraday_gap_true_for_same_session_missing_bars():
    # 同一盤（日盤）內，10:45 -> 13:45 中間漏了 11:45/12:45 兩根，算異常缺口。
    assert strategy_service._has_abnormal_intraday_gap(
        pd.Timestamp("2026-10-06 10:45"), pd.Timestamp("2026-10-06 13:45"), 60
    )


def test_has_abnormal_intraday_gap_false_for_exact_interval():
    # 正常相鄰一根，不算缺口。
    assert not strategy_service._has_abnormal_intraday_gap(
        pd.Timestamp("2026-10-06 10:45"), pd.Timestamp("2026-10-06 11:45"), 60
    )


def test_has_abnormal_intraday_gap_false_for_day_to_night_transition():
    # 日盤收盤(13:45) 跳到當晚第一根夜盤K棒(16:00)，是正常換盤，不算異常。
    assert not strategy_service._has_abnormal_intraday_gap(
        pd.Timestamp("2026-10-06 13:45"), pd.Timestamp("2026-10-06 16:00"), 60
    )


def test_has_abnormal_intraday_gap_false_for_night_to_next_day_across_holiday():
    # 跨假日：週五夜盤最後一根(05:00) 跳到下週一日盤第一根(09:45)，隔了好幾天，
    # 但是換盤的正常跳躍，不算異常。
    assert not strategy_service._has_abnormal_intraday_gap(
        pd.Timestamp("2026-10-09 05:00"), pd.Timestamp("2026-10-12 09:45"), 60
    )


def test_has_abnormal_intraday_gap_true_for_day_to_night_skipping_first_bars():
    """回歸測試（第一版的真實漏洞）：日盤收盤(13:45) 直接跳到夜盤第 6 根(21:00)，
    不是夜盤第一根(16:00)，中間漏了 16:00/17:00/18:00/19:00/20:00 五根——第一版
    的程式碼只檢查「盤別是否不同」，沒檢查「跳過去的是不是真的第一根」，會把這種
    情況誤判成正常換盤而放行。現在要求 cur 必須恰好等於新盤第一根的收盤時間。"""
    assert strategy_service._has_abnormal_intraday_gap(
        pd.Timestamp("2026-10-06 13:45"), pd.Timestamp("2026-10-06 21:00"), 60
    )


def test_has_abnormal_intraday_gap_true_for_night_to_day_skipping_first_bar():
    # 同理：夜盤收盤跳到日盤，但不是日盤第一根(09:45)，也算異常。
    assert strategy_service._has_abnormal_intraday_gap(
        pd.Timestamp("2026-10-06 05:00"), pd.Timestamp("2026-10-07 10:45"), 60
    )


def test_session_first_bar_time_generalizes_to_other_intervals():
    """回歸測試：Gemini 提出的替代方案把換盤第一根時間寫死成 60 分K 的
    16:00/09:45，套用到這個專案本來就有的 15 分K（death_cross 策略，見
    _interval_minutes_for_strategy）會整個判斷錯誤，把每天的正常換盤都誤判成
    異常缺口。這裡驗證 _session_first_bar_time 是用 interval_minutes 動態算，
    15 分K 的換盤第一根本來就該是 09:00（日盤）/15:15（夜盤），不是 60 分K 的
    09:45/16:00。"""
    assert strategy_service._session_first_bar_time("day", 60) == dt_time(9, 45)
    assert strategy_service._session_first_bar_time("night", 60) == dt_time(16, 0)
    assert strategy_service._session_first_bar_time("day", 15) == dt_time(9, 0)
    assert strategy_service._session_first_bar_time("night", 15) == dt_time(15, 15)
    # 用 15 分K 的真實換盤邊界驗證：13:45→15:15 是 15 分K 夜盤第一根，不算異常；
    # 13:45→16:00 對 15 分K 來說不是第一根（漏了 15:15~15:45 三根），算異常。
    assert not strategy_service._has_abnormal_intraday_gap(
        pd.Timestamp("2026-10-06 13:45"), pd.Timestamp("2026-10-06 15:15"), 15
    )
    assert strategy_service._has_abnormal_intraday_gap(
        pd.Timestamp("2026-10-06 13:45"), pd.Timestamp("2026-10-06 16:00"), 15
    )


def test_has_abnormal_gap_in_recent_window_catches_gap_further_back():
    """回歸測試（第一版的真實漏洞）：第一版只檢查「訊號用到的最後一對」，MA 卻是
    20 根的滾動視窗，缺口只要落在視窗裡任何一段就會讓均線失真。這裡建一個 bars，
    最後一對是正常的 60 分鐘間隔，但視窗再往前一點（gap_idx-1 再往前一步）有個同
    一盤內的異常缺口，驗證 _has_abnormal_gap_in_recent_window 抓得到（單看最後一對
    的舊版邏輯抓不到）。"""
    index = [
        pd.Timestamp("2026-10-06 09:45"),
        pd.Timestamp("2026-10-06 10:45"),
        pd.Timestamp("2026-10-06 13:45"),  # 跟上一根之間漏了 11:45/12:45，同盤內異常缺口
        pd.Timestamp("2026-10-06 14:45"),
        pd.Timestamp("2026-10-06 15:45"),  # 這一對（14:45→15:45）本身是正常的 60 分鐘間隔
    ]
    bars = pd.DataFrame({"close": range(len(index))}, index=pd.DatetimeIndex(index))
    gap_idx = len(bars) - 2  # 指向 14:45 這一根
    # 單看最後一對（13:45→14:45）的話是正常間隔，抓不到更前面 10:45→13:45 的缺口。
    assert not strategy_service._has_abnormal_intraday_gap(
        pd.Timestamp(bars.index[gap_idx - 1]), pd.Timestamp(bars.index[gap_idx]), 60
    )
    # 但整個視窗一起檢查，就能抓到。
    assert strategy_service._has_abnormal_gap_in_recent_window(bars, gap_idx, window=4, interval_minutes=60)


def _bars_with_gap_before_last_two_rows(*, abnormal_gap: bool) -> pd.DataFrame:
    """建 62 根（= ma_slow(60)+2，滿足 _min_bars_required_for_strategy）的真實
    DataFrame，缺口（或沒有缺口）刻意放在倒數第二、第三根之間——這正是
    _latest_closed_signal 用來判斷訊號的那一對（見 _tick_one 裡的 gap_idx）。"""
    warm_up = pd.date_range("2026-10-06 09:00", periods=60, freq="1min")
    last_normal = warm_up[-1]
    if abnormal_gap:
        second_last = last_normal + pd.Timedelta(hours=3)  # 同一盤內異常跳躍
    else:
        second_last = last_normal + pd.Timedelta(minutes=60)  # 正常間隔
    last = second_last + pd.Timedelta(minutes=60)
    index = list(warm_up) + [second_last, last]
    return pd.DataFrame({"close": range(len(index))}, index=pd.DatetimeIndex(index))


def test_tick_one_skips_signal_when_bars_have_abnormal_gap(clean_armed):
    """核心回歸測試：bars 倒數第二、三根之間有同一盤內的異常缺口時，就算
    _latest_closed_signal 回報 long 訊號，_tick_one 也不該下單（可能是缺口造成的
    假訊號），要先略過等下一輪缺口補齊。"""
    state = _open_long_state(
        product_code="TM2609", held_qty=0, held_direction=None, entry_price=0.0
    )
    state.last_signal_key = None
    svc = MagicMock()
    svc.place_order.return_value = {"order_result": {"success": True}}
    st = {"quote": {"last_price": 20000.0}}

    bars = _bars_with_gap_before_last_two_rows(abnormal_gap=True)

    with patch.object(
        strategy_service,
        "_latest_closed_signal",
        return_value={"time": "2026-10-06T10:00:00", "direction": "long", "key": "k1"},
    ), patch.object(
        strategy_service, "_now", return_value=pd.Timestamp("2026-10-06T10:00:00") + pd.Timedelta(minutes=5)
    ), patch.object(strategy_service, "_load_bars", return_value=bars):
        _tick_one(state, svc, st)

    svc.place_order.assert_not_called()
    assert state.held_qty == 0
    assert "缺口" in state.last_action


def test_tick_one_still_trades_when_bars_have_no_abnormal_gap(clean_armed):
    """對照組：同樣是真實 DataFrame（不是 MagicMock），但倒數第二、三根之間是正常
    間隔、沒有缺口——不該被新加的缺口防呆擋下，訊號照常執行進場。"""
    state = _open_long_state(
        product_code="TM2609", held_qty=0, held_direction=None, entry_price=0.0
    )
    state.last_signal_key = None
    svc = MagicMock()
    svc.place_order.return_value = {"order_result": {"success": True}}
    svc.place_oco_order.return_value = _oco_success_response()
    st = {"quote": {"last_price": 20000.0}}

    bars = _bars_with_gap_before_last_two_rows(abnormal_gap=False)

    with patch.object(
        strategy_service,
        "_latest_closed_signal",
        return_value={"time": "2026-10-06T10:00:00", "direction": "long", "key": "k1"},
    ), patch.object(
        strategy_service, "_now", return_value=pd.Timestamp("2026-10-06T10:00:00") + pd.Timedelta(minutes=5)
    ), patch.object(strategy_service, "_load_bars", return_value=bars), patch.object(
        strategy_service, "_signaled_frame", return_value=MagicMock()
    ):
        _tick_one(state, svc, st)

    svc.place_order.assert_called_once()
    assert state.held_qty == 1


def test_tick_one_skips_stale_signal_and_marks_it_consumed(clean_armed):
    """2026-10-07 加固（Gemini 提出的第二層防護）：就算缺口檢查都通過，訊號收盤
    時間如果離現在已經超過 1.5 個週期（表示是好幾輪前就該處理、卻拖到現在才真正
    算到的舊訊號），也不該追價下單——只標記為已消費，安靜略過。"""
    state = _open_long_state(
        product_code="TM2609", held_qty=0, held_direction=None, entry_price=0.0
    )
    state.last_signal_key = None
    svc = MagicMock()
    svc.place_order.return_value = {"order_result": {"success": True}}
    st = {"quote": {"last_price": 20000.0}}

    fake_bars = MagicMock()
    fake_bars.empty = False
    fake_bars.__len__.return_value = 100

    stale_signal_time = "2026-10-06T10:00:00"
    with patch.object(
        strategy_service,
        "_latest_closed_signal",
        return_value={"time": stale_signal_time, "direction": "long", "key": "k1"},
    ), patch.object(
        # "現在" 設在訊號收盤之後 3 小時——遠超過 60 分鐘策略的 1.5 個週期門檻。
        strategy_service, "_now", return_value=pd.Timestamp(stale_signal_time) + pd.Timedelta(hours=3)
    ), patch.object(strategy_service, "_load_bars", return_value=fake_bars), patch.object(
        strategy_service, "_signaled_frame", return_value=MagicMock()
    ):
        _tick_one(state, svc, st)

    svc.place_order.assert_not_called()
    assert state.held_qty == 0
    assert state.last_signal_key == "k1"  # 標記已消費，避免下一輪又重新評估同一個舊訊號
    assert "過期" in state.last_action


def _death_cross_short_state(**overrides) -> StrategyState:
    defaults = dict(
        strategy_id="death_cross_short",
        product_code="TM2609",
        strategy="death_cross",
        direction_limit="short",
        label="死叉做空",
        qty=1,
        initial_capital_ntd=100_000,
        max_loss_ntd=10_000,
        max_loss_pct=0.99,
        stop_loss_points=130.0,
        take_profit_points=130.0,
        reverse_signal_exit_enabled=False,
    )
    defaults.update(overrides)
    return StrategyState(**defaults)


def test_tick_one_death_cross_enters_short_via_signal(clean_armed):
    """跟現有 test_tick_one_entry_places_oco_protection 同款手法：mock
    _latest_closed_signal/_load_bars/_signaled_frame，驗證死叉策略走同一套
    generic 進場+OCO 機制，不需要真的產生死叉訊號的 K 棒資料。"""
    state = _death_cross_short_state(held_qty=0, held_direction=None, entry_price=0.0)
    state.last_signal_key = None
    svc = MagicMock()
    svc.place_order.return_value = {"order_result": {"success": True}}
    svc.place_oco_order.return_value = _oco_success_response()
    st = {"quote": {"last_price": 21400.0}}

    # 建構足夠的 fake bars，避免 quote staleness 檢查時把報價當成過期
    bar_count = strategy_service.DEFAULT_STRATEGY.ma_slow + 2
    fake_bars = pd.DataFrame(
        {
            "open": [21400.0] * bar_count,
            "high": [21450.0] * bar_count,
            "low": [21350.0] * bar_count,
            "close": [21400.0] * bar_count,
        }
    )

    with patch.object(
        strategy_service,
        "_latest_closed_signal",
        return_value={"time": "2026-08-25T09:00:00", "direction": "short", "key": "dc-1"},
    ), patch.object(
        strategy_service, "_now", return_value=pd.Timestamp("2026-08-25T09:00:00") + pd.Timedelta(minutes=5)
    ), patch.object(strategy_service, "_load_bars", return_value=fake_bars), patch.object(
        strategy_service, "_signaled_frame", return_value=MagicMock()
    ):
        _tick_one(state, svc, st)

    assert state.held_qty == 1
    assert state.held_direction == "short"
    assert state.entry_price == pytest.approx(21400.0)
    svc.place_oco_order.assert_called_once()
    assert state.protection_order_smart_key == "26564233"


def test_tick_one_death_cross_ignores_golden_cross_when_reverse_exit_disabled(clean_armed):
    """跟現有 test_tick_one_reverse_signal_skipped_when_disabled 同款手法：已持有
    死叉空單，遇到金叉（"long"）訊號，reverse_signal_exit_enabled=False 時應該
    完全不動作，只靠 130 點 SL/TP 出場。"""
    state = _death_cross_short_state(
        held_qty=1, held_direction="short", entry_price=21500.0,
    )
    state.last_signal_key = "previous-key"
    svc = MagicMock()
    st = {"quote": {"last_price": 21500.0}}  # 沒有觸及130點SL/TP，不會被那條路徑攔截

    fake_bars = MagicMock()
    fake_bars.empty = False
    fake_bars.__len__.return_value = 100

    with patch.object(
        strategy_service,
        "_latest_closed_signal",
        return_value={"time": "2026-08-25T09:15:00", "direction": "long", "key": "dc-golden-1"},
    ), patch.object(
        strategy_service, "_now", return_value=pd.Timestamp("2026-08-25T09:15:00") + pd.Timedelta(minutes=5)
    ), patch.object(strategy_service, "_load_bars", return_value=fake_bars), patch.object(
        strategy_service, "_signaled_frame", return_value=MagicMock()
    ):
        _tick_one(state, svc, st)

    svc.place_order.assert_not_called()
    assert state.held_qty == 1
    assert state.held_direction == "short"
    assert "已關閉反向訊號出場" in state.last_action


def test_klines_with_signals_60min_default_has_no_death_cross_field():
    fake_bars = pd.DataFrame(
        {
            "open": [100.0], "high": [100.0], "low": [100.0], "close": [100.0],
            "ma_fast": [100.0], "ma_mid": [100.0], "ma_slow": [100.0],
        },
        index=pd.date_range("2024-01-02 09:45:00", periods=1, freq="60min"),
    )

    with patch.object(strategy_service, "_load_bars", return_value=fake_bars) as mock_load, \
         patch.object(strategy_service, "_signaled_frame", return_value=fake_bars.assign(signal=[None])):
        rows = klines_with_signals("TM2609")

    assert mock_load.call_args.kwargs["interval_minutes"] == 60
    assert len(rows) == 1
    assert "death_cross_signal" not in rows[0]


def test_klines_with_signals_15min_includes_death_cross_field():
    fake_bars = pd.DataFrame(
        {
            "open": [100.0], "high": [100.0], "low": [100.0], "close": [100.0],
            "ma_fast": [100.0], "ma_mid": [100.0], "ma_slow": [100.0],
        },
        index=pd.date_range("2024-01-02 09:00:00", periods=1, freq="15min"),
    )

    with patch.object(strategy_service, "_load_bars", return_value=fake_bars) as mock_load, \
         patch.object(strategy_service, "_signaled_frame", return_value=fake_bars.assign(signal=[None])):
        rows = klines_with_signals("TM2609", interval_minutes=15)

    assert mock_load.call_args.kwargs["interval_minutes"] == 15
    assert mock_load.call_args.kwargs["min_bars_required"] == 22
    assert len(rows) == 1
    assert "death_cross_signal" in rows[0]