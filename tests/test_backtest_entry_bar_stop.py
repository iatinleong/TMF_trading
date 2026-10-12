"""進場當根 K 棒就穿過停損時，要在停損價出場，而不是等下一根開盤才出場。

2026-07-02 / 07-03 死叉空單實測：進場 K 棒（開 46066）的最高價 46467 已經超過停損 46196，
舊版引擎跳過進場當根，要到下一根開盤 46354 才出場，虧 288 點而不是停損的 130 點。
"""

import pandas as pd

from backend.backtest_engine import run_direction_limited_backtest


def _short_frame(bars: list[tuple[float, float, float, float]], signals: list[str]) -> pd.DataFrame:
    index = pd.date_range("2026-07-02 09:00", periods=len(bars), freq="15min")
    frame = pd.DataFrame(
        [{"open": o, "high": h, "low": l, "close": c} for o, h, l, c in bars],
        index=index,
    )
    frame["signal"] = signals
    # 引擎只要看到這三個欄位就不會再依策略重算訊號（測試只驗證出場邏輯）
    frame["signal_color"] = ""
    frame["signal_bar_close_time"] = index
    return frame


def test_entry_bar_high_through_short_stop_exits_at_stop_on_same_bar():
    # bar0 出現空單訊號；bar1 開盤 100 進場，停損 10 點 → 110，bar1 最高 115 已穿過停損
    frame = _short_frame(
        bars=[(100, 101, 99, 100), (100, 115, 95, 105), (105, 106, 104, 105)],
        signals=["short", "none", "none"],
    )
    trades, _ = run_direction_limited_backtest(
        frame,
        strategy="death_cross",
        direction_limit="short",
        stop_loss_points=10.0,
        take_profit_points=300.0,
        use_stop_take=True,
        use_risk_stop=False,
        reverse_signal_exit_enabled=False,
    )

    assert len(trades) == 1
    trade = trades[0]
    assert trade.exit_reason == "stop_loss"
    assert trade.exit_time == pd.Timestamp("2026-07-02 09:15")
    assert trade.exit_price == 110
    assert trade.gross_pnl_points == -10


def test_entry_bar_without_breach_is_not_exited_early():
    frame = _short_frame(
        bars=[(100, 101, 99, 100), (100, 105, 95, 103), (103, 104, 98, 99)],
        signals=["short", "none", "none"],
    )
    trades, _ = run_direction_limited_backtest(
        frame,
        strategy="death_cross",
        direction_limit="short",
        stop_loss_points=10.0,
        take_profit_points=300.0,
        use_stop_take=True,
        use_risk_stop=False,
        reverse_signal_exit_enabled=False,
    )

    assert len(trades) == 1
    assert trades[0].exit_reason == "end_of_data"
