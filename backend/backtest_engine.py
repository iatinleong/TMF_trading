from __future__ import annotations

"""
Core execution engine for Taiwan Index Futures (台指期) backtests.

Design decisions carried over from prior research and adapted for TAIFEX:
1. Fees/tax are charged on both entry and exit because 台指期 has real per-side
   commission plus futures transaction tax, unlike many crypto backtests that
   omit tax entirely.
2. Signal execution is delayed to the next bar's open to prevent look-ahead
   bias because signals are only known after bar-close confirmation.
3. Stop-loss/take-profit checks use each bar's high/low, and if both could be
   hit in the same bar we pessimistically assume the stop-loss fills first.
4. Gap-through stop/take levels fill at the bar open (market) rather than the
   theoretical stop/take level because 台指期 has day/night sessions and real
   overnight gaps, unlike 24/7 crypto markets.
5. Signal-to-fill logic lives in one reusable execution path so future
   backtest/paper/live layers can share the same behavior instead of drifting.
"""

from dataclasses import asdict, dataclass
from typing import Literal

import pandas as pd

try:
    from .config import (
        DEFAULT_CONTRACT,
        DEFAULT_COST,
        DEFAULT_STRATEGY,
        ContractSpec,
        CostConfig,
        StrategyConfig,
    )
    from .signals import generate_breakout_signals, generate_pullback_signals
except ImportError:  # pragma: no cover - script execution fallback
    from config import (  # type: ignore
        DEFAULT_CONTRACT,
        DEFAULT_COST,
        DEFAULT_STRATEGY,
        ContractSpec,
        CostConfig,
        StrategyConfig,
    )
    from signals import generate_breakout_signals, generate_pullback_signals  # type: ignore


Direction = Literal["long", "short"]
ExitReason = Literal["stop_loss", "take_profit", "immediate_reverse", "signal_exit", "end_of_data"]
StrategyName = Literal["breakout", "pullback"]

_PRICE_COLUMNS = ("open", "high", "low", "close")
_SIGNAL_COLUMNS = ("signal", "signal_color", "signal_bar_close_time")


@dataclass
class Trade:
    entry_time: pd.Timestamp
    entry_price: float
    direction: Direction
    exit_time: pd.Timestamp
    exit_price: float
    exit_reason: ExitReason
    stop_loss_price: float
    take_profit_price: float
    gross_pnl_points: float
    gross_pnl_ntd: float
    total_cost_ntd: float
    net_pnl_ntd: float


@dataclass
class _OpenPosition:
    entry_time: pd.Timestamp
    entry_price: float
    direction: Direction
    stop_loss_price: float
    take_profit_price: float


class BacktestEngine:
    def __init__(
        self,
        strategy: StrategyName,
        *,
        contract: ContractSpec,
        cost: CostConfig,
        strategy_cfg: StrategyConfig,
        slippage_points: float,
    ) -> None:
        if strategy not in {"breakout", "pullback"}:
            raise ValueError("strategy must be 'breakout' or 'pullback'.")

        self.strategy = strategy
        self.contract = contract
        self.cost = cost
        self.strategy_cfg = strategy_cfg
        self.slippage_points = float(slippage_points)
        self.stop_loss_points, self.take_profit_points = self._resolve_risk_parameters(strategy)

    def run(self, df: pd.DataFrame) -> tuple[list[Trade], dict]:
        prepared = self._prepare_dataframe(df)
        trades: list[Trade] = []
        position: _OpenPosition | None = None

        for index in range(len(prepared)):
            bar = prepared.iloc[index]
            bar_time = pd.Timestamp(prepared.index[index])

            if position is not None:
                pending = self._pending_reverse_signal(prepared, index)
                if pending is not None:
                    trade = self._close_trade(position, bar_time, pending["exit_price"], "immediate_reverse")
                    trades.append(trade)
                    position = self._open_position(
                        entry_time=bar_time,
                        entry_price=pending["entry_price"],
                        direction=pending["new_direction"],
                    )

            if position is None:
                pending_entry = self._pending_entry_signal(prepared, index)
                if pending_entry is not None:
                    position = self._open_position(
                        entry_time=bar_time,
                        entry_price=pending_entry["entry_price"],
                        direction=pending_entry["direction"],
                    )

            if position is not None:
                exit_event = self._evaluate_exit_on_bar(position, bar)
                if exit_event is not None:
                    trade = self._close_trade(position, bar_time, exit_event["exit_price"], exit_event["reason"])
                    trades.append(trade)
                    position = None

        if position is not None:
            last_time = pd.Timestamp(prepared.index[-1])
            last_close = float(prepared.iloc[-1]["close"])
            trades.append(
                self._close_trade(
                    position,
                    last_time,
                    self._apply_slippage(last_close, position.direction, is_entry=False),
                    "end_of_data",
                )
            )

        return trades, self._build_summary(trades)

    def _prepare_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        prepared = df.copy()
        if "datetime" in prepared.columns:
            prepared["datetime"] = pd.to_datetime(prepared["datetime"])
            prepared = prepared.set_index("datetime")

        if not isinstance(prepared.index, pd.DatetimeIndex):
            raise TypeError("df must have a DatetimeIndex or a 'datetime' column.")

        missing_prices = [column for column in _PRICE_COLUMNS if column not in prepared.columns]
        if missing_prices:
            raise ValueError(f"Missing required OHLC columns: {missing_prices}")

        if any(column not in prepared.columns for column in _SIGNAL_COLUMNS):
            prepared = self._generate_signals(prepared)
        elif self.strategy == "pullback" and "immediate_reverse" not in prepared.columns:
            raise ValueError("Pullback strategy requires an 'immediate_reverse' column.")

        prepared.index = pd.to_datetime(prepared.index)
        prepared = prepared.sort_index()
        return prepared

    def _generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        if self.strategy == "breakout":
            return generate_breakout_signals(df)
        return generate_pullback_signals(df)

    def _resolve_risk_parameters(self, strategy: StrategyName) -> tuple[float, float]:
        if strategy == "breakout":
            return (
                float(self.strategy_cfg.breakout_stop_loss_points),
                float(self.strategy_cfg.breakout_take_profit_points),
            )
        return (
            float(self.strategy_cfg.pullback_stop_loss_points),
            float(self.strategy_cfg.pullback_take_profit_points),
        )

    def _pending_entry_signal(self, df: pd.DataFrame, current_index: int) -> dict | None:
        if current_index == 0:
            return None

        signal_row = df.iloc[current_index - 1]
        direction = signal_row.get("signal")
        if direction not in {"long", "short"}:
            return None

        bar = df.iloc[current_index]
        return {
            "direction": direction,
            "entry_price": self._apply_slippage(float(bar["open"]), direction, is_entry=True),
        }

    def _pending_reverse_signal(self, df: pd.DataFrame, current_index: int) -> dict | None:
        if self.strategy != "pullback" or current_index == 0:
            return None

        signal_row = df.iloc[current_index - 1]
        direction = signal_row.get("signal")
        immediate_reverse = bool(signal_row.get("immediate_reverse", False))
        if direction not in {"long", "short"} or not immediate_reverse:
            return None

        bar = df.iloc[current_index]
        raw_open = float(bar["open"])
        return {
            "new_direction": direction,
            "entry_price": self._apply_slippage(raw_open, direction, is_entry=True),
            "exit_price": self._apply_slippage(raw_open, direction, is_entry=True),
        }

    def _open_position(self, *, entry_time: pd.Timestamp, entry_price: float, direction: Direction) -> _OpenPosition:
        if direction == "long":
            stop_loss_price = entry_price - self.stop_loss_points
            take_profit_price = entry_price + self.take_profit_points
        else:
            stop_loss_price = entry_price + self.stop_loss_points
            take_profit_price = entry_price - self.take_profit_points

        return _OpenPosition(
            entry_time=entry_time,
            entry_price=float(entry_price),
            direction=direction,
            stop_loss_price=float(stop_loss_price),
            take_profit_price=float(take_profit_price),
        )

    def _evaluate_exit_on_bar(self, position: _OpenPosition, bar: pd.Series) -> dict | None:
        open_price = float(bar["open"])
        high_price = float(bar["high"])
        low_price = float(bar["low"])

        if position.direction == "long":
            if open_price <= position.stop_loss_price:
                return {"reason": "stop_loss", "exit_price": self._apply_slippage(open_price, "long", is_entry=False)}
            if open_price >= position.take_profit_price:
                return {"reason": "take_profit", "exit_price": self._apply_slippage(open_price, "long", is_entry=False)}

            stop_hit = low_price <= position.stop_loss_price
            take_hit = high_price >= position.take_profit_price
            if stop_hit:
                return {
                    "reason": "stop_loss",
                    "exit_price": self._apply_slippage(position.stop_loss_price, "long", is_entry=False),
                }
            if take_hit:
                return {
                    "reason": "take_profit",
                    "exit_price": self._apply_slippage(position.take_profit_price, "long", is_entry=False),
                }
            return None

        if open_price >= position.stop_loss_price:
            return {"reason": "stop_loss", "exit_price": self._apply_slippage(open_price, "short", is_entry=False)}
        if open_price <= position.take_profit_price:
            return {"reason": "take_profit", "exit_price": self._apply_slippage(open_price, "short", is_entry=False)}

        stop_hit = high_price >= position.stop_loss_price
        take_hit = low_price <= position.take_profit_price
        if stop_hit:
            return {
                "reason": "stop_loss",
                "exit_price": self._apply_slippage(position.stop_loss_price, "short", is_entry=False),
            }
        if take_hit:
            return {
                "reason": "take_profit",
                "exit_price": self._apply_slippage(position.take_profit_price, "short", is_entry=False),
            }
        return None

    def _apply_slippage(self, raw_price: float, direction: Direction, *, is_entry: bool) -> float:
        if self.slippage_points == 0.0:
            return float(raw_price)

        if direction == "long":
            return float(raw_price + self.slippage_points) if is_entry else float(raw_price - self.slippage_points)
        return float(raw_price - self.slippage_points) if is_entry else float(raw_price + self.slippage_points)

    def _close_trade(
        self,
        position: _OpenPosition,
        exit_time: pd.Timestamp,
        exit_price: float,
        exit_reason: ExitReason,
    ) -> Trade:
        direction_sign = 1.0 if position.direction == "long" else -1.0
        gross_pnl_points = (float(exit_price) - position.entry_price) * direction_sign
        gross_pnl_ntd = gross_pnl_points * float(self.contract.point_value)

        entry_tax = position.entry_price * float(self.contract.point_value) * float(self.cost.tax_rate)
        exit_tax = float(exit_price) * float(self.contract.point_value) * float(self.cost.tax_rate)
        total_cost_ntd = (2.0 * float(self.cost.commission_per_side)) + entry_tax + exit_tax
        net_pnl_ntd = gross_pnl_ntd - total_cost_ntd

        return Trade(
            entry_time=position.entry_time,
            entry_price=position.entry_price,
            direction=position.direction,
            exit_time=exit_time,
            exit_price=float(exit_price),
            exit_reason=exit_reason,
            stop_loss_price=position.stop_loss_price,
            take_profit_price=position.take_profit_price,
            gross_pnl_points=float(gross_pnl_points),
            gross_pnl_ntd=float(gross_pnl_ntd),
            total_cost_ntd=float(total_cost_ntd),
            net_pnl_ntd=float(net_pnl_ntd),
        )

    def _build_summary(self, trades: list[Trade]) -> dict:
        total_trades = len(trades)
        net_pnls = [trade.net_pnl_ntd for trade in trades]
        wins = [pnl for pnl in net_pnls if pnl > 0]
        losses = [pnl for pnl in net_pnls if pnl < 0]

        cumulative = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for pnl in net_pnls:
            cumulative += pnl
            peak = max(peak, cumulative)
            max_drawdown = max(max_drawdown, peak - cumulative)

        if losses:
            profit_factor = sum(wins) / abs(sum(losses)) if wins else 0.0
        else:
            profit_factor = float("inf") if wins else 0.0

        return {
            "total_trades": total_trades,
            "win_rate": (len(wins) / total_trades) if total_trades else 0.0,
            "total_net_pnl_ntd": float(sum(net_pnls)),
            "max_drawdown_ntd": float(max_drawdown),
            "profit_factor": float(profit_factor),
            "avg_win_ntd": float(sum(wins) / len(wins)) if wins else 0.0,
            "avg_loss_ntd": float(sum(losses) / len(losses)) if losses else 0.0,
        }


def run_backtest(
    df: pd.DataFrame,
    strategy: str,
    contract: ContractSpec = DEFAULT_CONTRACT,
    cost: CostConfig = DEFAULT_COST,
    strategy_cfg: StrategyConfig = DEFAULT_STRATEGY,
    slippage_points: float = 0.0,
) -> tuple[list[Trade], dict]:
    """
    Run the shared Taiwan futures execution engine.

    If signal columns are already present they are used as-is; otherwise this
    function generates the appropriate strategy signals internally so callers can
    pass either a pre-signaled frame or a plain indicator-enriched OHLC frame.
    Breakout positions ignore opposite signals while already in a trade; pullback
    positions alone may close-and-reverse when ``immediate_reverse`` is set.
    """

    engine = BacktestEngine(
        strategy=strategy,  # type: ignore[arg-type]
        contract=contract,
        cost=cost,
        strategy_cfg=strategy_cfg,
        slippage_points=slippage_points,
    )
    return engine.run(df)


def run_direction_limited_backtest(
    signaled: pd.DataFrame,
    *,
    strategy: StrategyName,
    direction_limit: Direction,
    contract: ContractSpec = DEFAULT_CONTRACT,
    cost: CostConfig = DEFAULT_COST,
    slippage_points: float = 0.0,
    stop_loss_points: float = 100.0,
    take_profit_points: float = 300.0,
    use_stop_take: bool = True,
    initial_capital_ntd: float = 100_000.0,
    max_loss_ntd: float = 10_000.0,
    max_loss_pct: float = 0.10,
    use_risk_stop: bool = True,
) -> tuple[list[Trade], dict]:
    """
    對齊 backend/strategy_service.py 實盤 4 策略邏輯的方向限定回測，2026-08-06 新增、
    2026-08-07 改成點數停損停利為主、NTD/%浮動損益門檻當「額外保險」：
    - 只在訊號方向與 direction_limit 相同時進場；已持倉時同向訊號忽略不動作。
    - 訊號方向相反時只出場、不反手（跟 experiments/sltp_vs_signal_only_ab_test.py 的
      signal_only 版本不同——那個會反手開反向倉，這裡完全比照實盤 direction_limit 語意）。
    - use_stop_take=True（預設）時，持倉觸及進場價 ±stop_loss_points/take_profit_points
      即出場。use_risk_stop=True（預設）時，同時比較 max_loss_ntd / max_loss_pct*
      initial_capital_ntd 換算成點數的門檻，取「先觸發的那個」（點數停損 vs
      風控保險，哪個價位先碰到就用哪個，比照實盤 _tick_one 的邏輯）。
    - 2026-08-07 修正跳空處理：每根K棒先檢查「開盤價」有沒有已經跳空穿過停損/停利/
      風控價位（例如日盤夜盤中間跳空），有的話用開盤價成交，不能假設剛好成交在
      理論價位；開盤沒跳空的話才用該根K棒 high/low 判斷盤中有沒有觸價，觸價一樣
      在理論價位成交。同一根K棒兩者都可能觸及時，保守假設停損（或風控保險，看哪個
      先觸發）先成交（沿用舊版固定 SL/TP 引擎 `_evaluate_exit_on_bar` 的悲觀假設）。
    """
    engine = BacktestEngine(
        strategy=strategy,
        contract=contract,
        cost=cost,
        strategy_cfg=DEFAULT_STRATEGY,
        slippage_points=slippage_points,
    )
    prepared = engine._prepare_dataframe(signaled)  # noqa: SLF001

    trades: list[Trade] = []
    position: _OpenPosition | None = None
    # 停損跟風控保險兩個候選價位取「先觸發的那個」算出來 position.stop_loss_price，
    # 這裡另外記錄「那個有效停損價實際上是哪一種」，出場時才能標對 exit_reason。
    # 全程只會有一個部位在跑，用簡單變數記，不需要用 id(position) 這種容易因記憶體
    # 位址重複使用而出錯的 key。
    active_stop_reason: str = "stop_loss"

    def _risk_stop_points() -> float:
        loss_ntd_limit = abs(float(max_loss_ntd))
        loss_pct_limit_ntd = abs(float(max_loss_pct)) * float(initial_capital_ntd)
        return min(loss_ntd_limit, loss_pct_limit_ntd) / float(contract.point_value)

    for index in range(len(prepared)):
        bar = prepared.iloc[index]
        bar_time = pd.Timestamp(prepared.index[index])

        if position is not None and (use_stop_take or use_risk_stop):
            open_price = float(bar["open"])
            high_price = float(bar["high"])
            low_price = float(bar["low"])
            stop_reason = active_stop_reason

            if position.direction == "long":
                if open_price <= position.stop_loss_price:
                    exit_price = engine._apply_slippage(open_price, "long", is_entry=False)  # noqa: SLF001
                    trades.append(engine._close_trade(position, bar_time, exit_price, stop_reason))  # noqa: SLF001
                    position = None
                elif use_stop_take and open_price >= position.take_profit_price:
                    exit_price = engine._apply_slippage(open_price, "long", is_entry=False)  # noqa: SLF001
                    trades.append(engine._close_trade(position, bar_time, exit_price, "take_profit"))  # noqa: SLF001
                    position = None
                elif low_price <= position.stop_loss_price:
                    exit_price = engine._apply_slippage(position.stop_loss_price, "long", is_entry=False)  # noqa: SLF001
                    trades.append(engine._close_trade(position, bar_time, exit_price, stop_reason))  # noqa: SLF001
                    position = None
                elif use_stop_take and high_price >= position.take_profit_price:
                    exit_price = engine._apply_slippage(position.take_profit_price, "long", is_entry=False)  # noqa: SLF001
                    trades.append(engine._close_trade(position, bar_time, exit_price, "take_profit"))  # noqa: SLF001
                    position = None
            else:
                if open_price >= position.stop_loss_price:
                    exit_price = engine._apply_slippage(open_price, "short", is_entry=False)  # noqa: SLF001
                    trades.append(engine._close_trade(position, bar_time, exit_price, stop_reason))  # noqa: SLF001
                    position = None
                elif use_stop_take and open_price <= position.take_profit_price:
                    exit_price = engine._apply_slippage(open_price, "short", is_entry=False)  # noqa: SLF001
                    trades.append(engine._close_trade(position, bar_time, exit_price, "take_profit"))  # noqa: SLF001
                    position = None
                elif high_price >= position.stop_loss_price:
                    exit_price = engine._apply_slippage(position.stop_loss_price, "short", is_entry=False)  # noqa: SLF001
                    trades.append(engine._close_trade(position, bar_time, exit_price, stop_reason))  # noqa: SLF001
                    position = None
                elif use_stop_take and low_price <= position.take_profit_price:
                    exit_price = engine._apply_slippage(position.take_profit_price, "short", is_entry=False)  # noqa: SLF001
                    trades.append(engine._close_trade(position, bar_time, exit_price, "take_profit"))  # noqa: SLF001
                    position = None

        if index > 0:
            signal_row = prepared.iloc[index - 1]
            direction = signal_row.get("signal")
            if direction in ("long", "short"):
                current_open = float(bar["open"])
                if direction == direction_limit:
                    if position is None:
                        entry_price = engine._apply_slippage(current_open, direction, is_entry=True)  # noqa: SLF001
                        point_stop = abs(stop_loss_points) if use_stop_take else float("inf")
                        risk_stop = _risk_stop_points() if use_risk_stop else float("inf")
                        # 取「先觸發」的那個，也就是點數比較小（比較容易先被碰到）的一個。
                        effective_stop_points = min(point_stop, risk_stop)
                        stop_reason = "stop_loss" if point_stop <= risk_stop else "risk_stop"
                        if effective_stop_points == float("inf"):
                            stop_loss_price = float("nan")
                        elif direction == "long":
                            stop_loss_price = float(entry_price) - effective_stop_points
                        else:
                            stop_loss_price = float(entry_price) + effective_stop_points
                        if use_stop_take:
                            take_profit_price = (
                                float(entry_price) + abs(take_profit_points)
                                if direction == "long"
                                else float(entry_price) - abs(take_profit_points)
                            )
                        else:
                            take_profit_price = float("nan")
                        position = _OpenPosition(
                            entry_time=bar_time,
                            entry_price=float(entry_price),
                            direction=direction,
                            stop_loss_price=stop_loss_price,
                            take_profit_price=take_profit_price,
                        )
                        active_stop_reason = stop_reason
                    # else: 同向已持倉，不動作（比照實盤「同向訊號不動作」）
                elif position is not None:
                    exit_price = engine._apply_slippage(current_open, position.direction, is_entry=False)  # noqa: SLF001
                    trades.append(engine._close_trade(position, bar_time, exit_price, "signal_exit"))  # noqa: SLF001
                    position = None
                # else: 空手遇反向訊號，不動作（比照實盤「本策略不做這個方向」）

    if position is not None:
        last_time = pd.Timestamp(prepared.index[-1])
        last_close = float(prepared.iloc[-1]["close"])
        exit_price = engine._apply_slippage(last_close, position.direction, is_entry=False)  # noqa: SLF001
        trades.append(engine._close_trade(position, last_time, exit_price, "end_of_data"))  # noqa: SLF001

    return trades, engine._build_summary(trades)  # noqa: SLF001


def trades_to_dataframe(trades: list[Trade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame(columns=[field.name for field in Trade.__dataclass_fields__.values()])
    return pd.DataFrame([asdict(trade) for trade in trades])
