import pandas as pd
import pytest

from backend.backtest_engine import run_backtest, run_direction_limited_backtest
from backend.config import ContractSpec, DEFAULT_CONTRACT, DEFAULT_COST, StrategyConfig
from backend.indicators import add_moving_averages
from backend.signals import generate_breakout_signals, generate_pullback_signals


def make_index(length: int) -> pd.DatetimeIndex:
    return pd.date_range("2024-01-02 09:45:00", periods=length, freq="60min")


def make_ohlc_frame(*, opens: list[float], highs: list[float], lows: list[float], closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [1] * len(closes),
        },
        index=make_index(len(closes)),
    )


def make_signaled_frame(
    *,
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    signals: list[str | None],
    signal_colors: list[str | None],
    immediate_reverse: list[bool] | None = None,
) -> pd.DataFrame:
    frame = make_ohlc_frame(opens=opens, highs=highs, lows=lows, closes=closes)
    frame["signal"] = signals
    frame["signal_color"] = signal_colors
    frame["signal_bar_close_time"] = frame.index
    if immediate_reverse is not None:
        frame["immediate_reverse"] = immediate_reverse
    return frame


@pytest.mark.parametrize(
    ("closes", "expected_signal", "expected_color"),
    [
        ([10.0, 9.0, 8.0, 9.0, 10.0], "long", "red"),
        ([8.0, 9.0, 10.0, 9.0, 8.0], "short", "green"),
    ],
)
def test_generate_breakout_signals_flags_exact_ma_crossover_bar(
    closes: list[float], expected_signal: str, expected_color: str
) -> None:
    frame = make_ohlc_frame(
        opens=closes,
        highs=[value + 0.5 for value in closes],
        lows=[value - 0.5 for value in closes],
        closes=closes,
    )
    enriched = add_moving_averages(frame, fast=2, mid=3, slow=4)

    signaled = generate_breakout_signals(enriched)

    signal_rows = signaled.loc[signaled["signal"].notna(), ["signal", "signal_color"]]
    assert list(signal_rows.index) == [signaled.index[4]]
    assert signal_rows.iloc[0]["signal"] == expected_signal
    assert signal_rows.iloc[0]["signal_color"] == expected_color


def test_run_backtest_enters_on_next_bar_open_not_signal_bar() -> None:
    frame = make_ohlc_frame(
        opens=[10.0, 9.0, 8.0, 9.0, 9.5, 12.0],
        highs=[10.5, 9.5, 8.5, 9.5, 10.5, 12.5],
        lows=[9.5, 8.5, 7.5, 8.5, 9.0, 11.5],
        closes=[10.0, 9.0, 8.0, 9.0, 10.0, 12.2],
    )
    enriched = add_moving_averages(frame, fast=2, mid=3, slow=4)
    signaled = generate_breakout_signals(enriched)

    trades, _ = run_backtest(signaled, strategy="breakout")

    assert len(trades) == 1
    trade = trades[0]
    signal_time = signaled.index[4]
    next_bar_time = signaled.index[5]
    assert trade.entry_time == next_bar_time
    assert trade.entry_price == pytest.approx(12.0)
    assert trade.entry_time != signal_time
    assert trade.entry_price != pytest.approx(float(signaled.loc[signal_time, "open"]))
    assert trade.entry_price != pytest.approx(float(signaled.loc[signal_time, "close"]))


def test_same_bar_stop_and_take_profit_for_long_uses_conservative_stop_loss() -> None:
    frame = make_signaled_frame(
        opens=[95.0, 100.0, 100.0],
        highs=[96.0, 105.0, 111.0],
        lows=[94.0, 95.0, 89.0],
        closes=[95.0, 100.0, 100.0],
        signals=["long", None, None],
        signal_colors=["red", None, None],
    )
    strategy_cfg = StrategyConfig(
        breakout_stop_loss_points=10.0,
        breakout_take_profit_points=10.0,
    )

    trades, _ = run_backtest(frame, strategy="breakout", strategy_cfg=strategy_cfg)

    assert len(trades) == 1
    trade = trades[0]
    assert trade.entry_price == pytest.approx(100.0)
    assert trade.exit_reason == "stop_loss"
    assert trade.exit_time == frame.index[2]
    assert trade.exit_price == pytest.approx(90.0)


def test_gap_through_stop_loss_fills_at_next_bar_open() -> None:
    frame = make_signaled_frame(
        opens=[95.0, 100.0, 85.0],
        highs=[96.0, 105.0, 95.0],
        lows=[94.0, 95.0, 80.0],
        closes=[95.0, 100.0, 86.0],
        signals=["long", None, None],
        signal_colors=["red", None, None],
    )
    strategy_cfg = StrategyConfig(
        breakout_stop_loss_points=10.0,
        breakout_take_profit_points=20.0,
    )

    trades, _ = run_backtest(frame, strategy="breakout", strategy_cfg=strategy_cfg)

    assert len(trades) == 1
    trade = trades[0]
    assert trade.exit_reason == "stop_loss"
    assert trade.exit_time == frame.index[2]
    assert trade.exit_price == pytest.approx(85.0)
    assert trade.exit_price != pytest.approx(90.0)


def test_trade_costs_and_net_pnl_match_hand_calculation() -> None:
    frame = make_signaled_frame(
        opens=[95.0, 100.0],
        highs=[96.0, 111.0],
        lows=[94.0, 99.0],
        closes=[95.0, 110.0],
        signals=["long", None],
        signal_colors=["red", None],
    )
    strategy_cfg = StrategyConfig(
        breakout_stop_loss_points=1000.0,
        breakout_take_profit_points=1000.0,
    )

    trades, _ = run_backtest(frame, strategy="breakout", strategy_cfg=strategy_cfg)

    assert len(trades) == 1
    trade = trades[0]
    expected_gross = (110.0 - 100.0) * DEFAULT_CONTRACT.point_value
    expected_cost = (
        2 * DEFAULT_COST.commission_per_side
        + (100.0 * DEFAULT_CONTRACT.point_value * DEFAULT_COST.tax_rate)
        + (110.0 * DEFAULT_CONTRACT.point_value * DEFAULT_COST.tax_rate)
    )
    expected_net = expected_gross - expected_cost

    assert trade.gross_pnl_ntd == pytest.approx(expected_gross)
    assert trade.total_cost_ntd == pytest.approx(expected_cost)
    assert trade.net_pnl_ntd == pytest.approx(expected_net)
    assert trade.net_pnl_ntd == pytest.approx(trade.gross_pnl_ntd - trade.total_cost_ntd)


def test_pullback_repeated_same_direction_touches_create_distinct_trades() -> None:
    frame = make_ohlc_frame(
        opens=[105.0, 104.0, 102.0, 103.0, 104.0, 103.0, 102.0, 103.0],
        highs=[107.0, 106.0, 104.0, 108.0, 105.0, 104.0, 103.0, 108.0],
        lows=[104.0, 99.0, 101.0, 102.0, 103.0, 99.0, 101.0, 102.0],
        closes=[105.0, 101.0, 103.0, 106.0, 104.0, 101.0, 102.0, 106.0],
    )
    frame["ma_mid"] = 100.0
    frame["ma_slow"] = 90.0

    signaled = generate_pullback_signals(frame)
    signal_rows = signaled.loc[signaled["signal"].notna(), ["signal", "immediate_reverse"]]

    assert list(signal_rows.index) == [frame.index[1], frame.index[5]]
    assert list(signal_rows["signal"]) == ["long", "long"]
    assert list(signal_rows["immediate_reverse"]) == [False, False]

    strategy_cfg = StrategyConfig(
        pullback_stop_loss_points=5.0,
        pullback_take_profit_points=5.0,
    )
    trades, summary = run_backtest(signaled, strategy="pullback", strategy_cfg=strategy_cfg)

    assert summary["total_trades"] == 2
    assert [trade.direction for trade in trades] == ["long", "long"]
    assert [trade.entry_time for trade in trades] == [frame.index[2], frame.index[6]]
    assert [trade.exit_reason for trade in trades] == ["take_profit", "take_profit"]


def test_generate_pullback_signals_marks_opposite_touch_as_immediate_reverse() -> None:
    frame = make_ohlc_frame(
        opens=[105.0, 104.0, 103.0, 96.0, 98.0],
        highs=[106.0, 106.0, 105.0, 98.0, 101.0],
        lows=[104.0, 99.0, 102.0, 94.0, 94.0],
        closes=[105.0, 101.0, 104.0, 95.0, 99.0],
    )
    frame["ma_mid"] = [100.0, 100.0, 100.0, 100.0, 100.0]
    frame["ma_slow"] = [90.0, 90.0, 90.0, 110.0, 110.0]

    signaled = generate_pullback_signals(frame)
    signal_rows = signaled.loc[signaled["signal"].notna(), ["signal", "immediate_reverse"]]

    assert list(signal_rows.index) == [frame.index[1], frame.index[4]]
    assert list(signal_rows["signal"]) == ["long", "short"]
    assert list(signal_rows["immediate_reverse"]) == [False, True]


def test_pullback_opposite_signal_immediately_closes_and_reverses_next_bar() -> None:
    frame = make_signaled_frame(
        opens=[99.0, 100.0, 103.0],
        highs=[100.0, 102.0, 104.0],
        lows=[98.0, 99.0, 102.0],
        closes=[99.5, 101.0, 103.0],
        signals=["long", "short", None],
        signal_colors=["red", "green", None],
        immediate_reverse=[False, True, False],
    )
    strategy_cfg = StrategyConfig(
        pullback_stop_loss_points=50.0,
        pullback_take_profit_points=50.0,
    )

    trades, _ = run_backtest(frame, strategy="pullback", strategy_cfg=strategy_cfg)

    assert len(trades) == 2

    first_trade = trades[0]
    assert first_trade.direction == "long"
    assert first_trade.entry_time == frame.index[1]
    assert first_trade.entry_price == pytest.approx(100.0)
    assert first_trade.exit_reason == "immediate_reverse"
    assert first_trade.exit_time == frame.index[2]
    assert first_trade.exit_price == pytest.approx(103.0)

    second_trade = trades[1]
    assert second_trade.direction == "short"
    assert second_trade.entry_time == frame.index[2]
    assert second_trade.entry_price == pytest.approx(103.0)
    assert second_trade.exit_time == frame.index[2]


_TMF_CONTRACT = ContractSpec(name="TMF", point_value=10.0, tick_size=1.0)


def test_direction_limited_backtest_ignores_opposite_entry_signal() -> None:
    # direction_limit="long"：空手時遇到反向(short)訊號，比照實盤「本策略不做這個方向」，不動作。
    frame = make_signaled_frame(
        opens=[10.0, 10.0, 10.0, 10.0, 10.0],
        highs=[10.5, 10.5, 10.5, 10.5, 10.5],
        lows=[9.5, 9.5, 9.5, 9.5, 9.5],
        closes=[10.0, 10.0, 10.0, 10.0, 10.0],
        signals=[None, "short", None, None, None],
        signal_colors=[None, "green", None, None, None],
    )

    trades, _ = run_direction_limited_backtest(
        frame, strategy="breakout", direction_limit="long", contract=_TMF_CONTRACT, cost=DEFAULT_COST,
    )

    assert trades == []


def test_direction_limited_backtest_ignores_repeated_same_direction_signal() -> None:
    # 已持倉時再度出現同向訊號，比照實盤「同向訊號不動作」，不會加碼或重新進場。
    frame = make_signaled_frame(
        opens=[10.0, 10.0, 11.0, 11.0, 12.0],
        highs=[10.5, 10.5, 11.5, 11.5, 12.5],
        lows=[9.5, 9.5, 10.5, 10.5, 11.5],
        closes=[10.0, 10.0, 11.0, 11.0, 12.0],
        signals=[None, "long", None, "long", None],
        signal_colors=[None, "red", None, "red", None],
    )

    trades, _ = run_direction_limited_backtest(
        frame, strategy="breakout", direction_limit="long", contract=_TMF_CONTRACT, cost=DEFAULT_COST,
        use_stop_take=False,
    )

    assert len(trades) == 1
    assert trades[0].entry_price == pytest.approx(11.0)
    assert trades[0].exit_reason == "end_of_data"


def test_direction_limited_backtest_exits_without_reversing_on_opposite_signal() -> None:
    # 反向訊號只出場，不像 sltp_vs_signal_only_ab_test 的 signal_only 版本那樣反手開新倉。
    frame = make_signaled_frame(
        opens=[10.0, 10.0, 11.0, 11.0, 9.0],
        highs=[10.5, 10.5, 11.5, 11.5, 9.5],
        lows=[9.5, 9.5, 10.5, 10.5, 8.5],
        closes=[10.0, 10.0, 11.0, 11.0, 9.0],
        signals=[None, "long", None, "short", None],
        signal_colors=[None, "red", None, "green", None],
    )

    trades, _ = run_direction_limited_backtest(
        frame, strategy="breakout", direction_limit="long", contract=_TMF_CONTRACT, cost=DEFAULT_COST,
        use_stop_take=False,
    )

    assert len(trades) == 1
    trade = trades[0]
    assert trade.direction == "long"
    assert trade.entry_price == pytest.approx(11.0)
    assert trade.exit_price == pytest.approx(9.0)
    assert trade.exit_reason == "signal_exit"


def test_direction_limited_backtest_stop_loss_triggers_on_bar_low() -> None:
    # stop_loss_points=100。訊號在index1，進場發生在index2開盤價100（停損價=100-100=0）；
    # 停損檢查在每輪一開始就跑（在index2當根開倉之前檢查過一次、當時還沒有部位），
    # 所以深跌要放在index3才會是「已持倉後」第一次被檢查到的那根K棒。
    frame = make_signaled_frame(
        opens=[100.0, 100.0, 100.0, 100.0],
        highs=[100.5, 100.5, 100.5, 100.5],
        lows=[99.5, 99.5, 99.5, -10.0],
        closes=[100.0, 100.0, 100.0, 100.0],
        signals=[None, "long", None, None],
        signal_colors=[None, "red", None, None],
    )

    trades, _ = run_direction_limited_backtest(
        frame, strategy="breakout", direction_limit="long", contract=_TMF_CONTRACT, cost=DEFAULT_COST,
        stop_loss_points=100.0, take_profit_points=300.0, use_stop_take=True,
    )

    assert len(trades) == 1
    trade = trades[0]
    assert trade.exit_reason == "stop_loss"
    assert trade.entry_price == pytest.approx(100.0)
    assert trade.exit_price == pytest.approx(0.0)
    assert trade.exit_time == frame.index[3]


def test_direction_limited_backtest_take_profit_triggers_on_bar_high() -> None:
    # take_profit_points=300。進場價100（停利價=100+300=400），index3的high衝到500會觸價。
    frame = make_signaled_frame(
        opens=[100.0, 100.0, 100.0, 100.0],
        highs=[100.5, 100.5, 100.5, 500.0],
        lows=[99.5, 99.5, 99.5, 99.5],
        closes=[100.0, 100.0, 100.0, 100.0],
        signals=[None, "long", None, None],
        signal_colors=[None, "red", None, None],
    )

    trades, _ = run_direction_limited_backtest(
        frame, strategy="breakout", direction_limit="long", contract=_TMF_CONTRACT, cost=DEFAULT_COST,
        stop_loss_points=100.0, take_profit_points=300.0, use_stop_take=True,
    )

    assert len(trades) == 1
    trade = trades[0]
    assert trade.exit_reason == "take_profit"
    assert trade.entry_price == pytest.approx(100.0)
    assert trade.exit_price == pytest.approx(400.0)
    assert trade.exit_time == frame.index[3]


def test_direction_limited_backtest_stop_take_disabled() -> None:
    # 同樣的深跌 bar，關掉停損停利就不該觸發，部位撐到 end_of_data 才平倉。
    frame = make_signaled_frame(
        opens=[100.0, 100.0, 100.0, 100.0],
        highs=[100.5, 100.5, 100.5, 100.5],
        lows=[99.5, 99.5, 99.5, -10.0],
        closes=[100.0, 100.0, 100.0, 100.0],
        signals=[None, "long", None, None],
        signal_colors=[None, "red", None, None],
    )

    trades, _ = run_direction_limited_backtest(
        frame, strategy="breakout", direction_limit="long", contract=_TMF_CONTRACT, cost=DEFAULT_COST,
        stop_loss_points=100.0, take_profit_points=300.0, use_stop_take=False,
    )

    assert len(trades) == 1
    assert trades[0].exit_reason == "end_of_data"


def test_direction_limited_backtest_gap_through_stop_fills_at_open() -> None:
    # entry=100（index2開盤），stop_loss_points=20→理論停損價80。index3開盤直接跳空到70，
    # 已經跳過80，應該用開盤價70成交，不能假裝剛好成交在理論停損價80。
    frame = make_signaled_frame(
        opens=[100.0, 100.0, 100.0, 70.0],
        highs=[100.5, 100.5, 100.5, 70.5],
        lows=[99.5, 99.5, 99.5, 65.0],
        closes=[100.0, 100.0, 100.0, 70.0],
        signals=[None, "long", None, None],
        signal_colors=[None, "red", None, None],
    )

    trades, _ = run_direction_limited_backtest(
        frame, strategy="breakout", direction_limit="long", contract=_TMF_CONTRACT, cost=DEFAULT_COST,
        stop_loss_points=20.0, take_profit_points=300.0, use_stop_take=True, use_risk_stop=False,
    )

    assert len(trades) == 1
    trade = trades[0]
    assert trade.exit_reason == "stop_loss"
    assert trade.exit_price == pytest.approx(70.0)
    assert trade.exit_price != pytest.approx(80.0)


def test_direction_limited_backtest_risk_stop_triggers_before_point_stop() -> None:
    # stop_loss_points=1000（很寬，不會被點數觸發）；max_loss_ntd=500，point_value=10
    # → risk_stop 門檻50點，entry=100 → risk_stop_price=50。index3 low跌到45（不跳空開盤），
    # 應該用 risk_stop 這個理由、在理論價位50成交。
    frame = make_signaled_frame(
        opens=[100.0, 100.0, 100.0, 100.0],
        highs=[100.5, 100.5, 100.5, 100.5],
        lows=[99.5, 99.5, 99.5, 45.0],
        closes=[100.0, 100.0, 100.0, 100.0],
        signals=[None, "long", None, None],
        signal_colors=[None, "red", None, None],
    )

    trades, _ = run_direction_limited_backtest(
        frame, strategy="breakout", direction_limit="long", contract=_TMF_CONTRACT, cost=DEFAULT_COST,
        stop_loss_points=1000.0, take_profit_points=3000.0, use_stop_take=True,
        initial_capital_ntd=100_000.0, max_loss_ntd=500.0, max_loss_pct=0.99, use_risk_stop=True,
    )

    assert len(trades) == 1
    trade = trades[0]
    assert trade.exit_reason == "risk_stop"
    assert trade.exit_price == pytest.approx(50.0)


def test_direction_limited_backtest_risk_stop_disabled() -> None:
    # 同樣的深跌情境，關掉 use_risk_stop 就不該觸發，部位撐到 end_of_data。
    frame = make_signaled_frame(
        opens=[100.0, 100.0, 100.0, 100.0],
        highs=[100.5, 100.5, 100.5, 100.5],
        lows=[99.5, 99.5, 99.5, 45.0],
        closes=[100.0, 100.0, 100.0, 100.0],
        signals=[None, "long", None, None],
        signal_colors=[None, "red", None, None],
    )

    trades, _ = run_direction_limited_backtest(
        frame, strategy="breakout", direction_limit="long", contract=_TMF_CONTRACT, cost=DEFAULT_COST,
        stop_loss_points=1000.0, take_profit_points=3000.0, use_stop_take=True,
        initial_capital_ntd=100_000.0, max_loss_ntd=500.0, max_loss_pct=0.99, use_risk_stop=False,
    )

    assert len(trades) == 1
    assert trades[0].exit_reason == "end_of_data"
