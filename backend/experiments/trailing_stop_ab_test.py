"""
trailing_stop_ab_test.py — 測試「移動停損 (trailing stop)」是否能改善回測/回彈策略

使用者構想：把固定停利300點放寬到600點，同時把停損150點改成「只能往有利方向移動、
不能往回退」的移動停損（類似海龜交易的 chandelier trailing stop）。

為了避免 look-ahead bias，移動停損的更新方式與核心引擎「訊號在K棒收盤確認、於下一根
K棒才執行」的因果原則一致：每一根K棒要不要移動停損，只根據「前一根已收盤K棒」為止
的最高價/最低價來決定，不會用當前這根尚未評估完的K棒的走勢來預先移動停損，
確保跟正式回測引擎一樣不會偷看未來。

同根K棒停損/停利衝突時，一律假設停損優先觸發（悲觀假設，與 backtest_engine.py 一致）。
跳空時以開盤價（市價）成交，不用理論停損/停利價位（與 backtest_engine.py 一致）。

測試的變體：
    baseline        現行固定 SL150 / TP300（無移動停損）
    wider_tp        固定 SL150 / TP600（無移動停損，純粹放寬停利）
    trailing_capped 移動停損（起始150，只前進不後退）+ 停利600（有上限）
    trailing_open   移動停損（起始150，只前進不後退）+ 無停利上限（讓獲利奔跑，只靠移動停損出場）

輸出：data/experiments/trailing_stop_ab_results.md / .json
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

try:
    from ..config import ContractSpec, DEFAULT_STRATEGY, cost_config_for_contract
    from ..indicators import add_moving_averages
    from ..signals import generate_pullback_signals
except ImportError:  # pragma: no cover - script execution fallback
    from config import ContractSpec, DEFAULT_STRATEGY, cost_config_for_contract  # type: ignore
    from indicators import add_moving_averages  # type: ignore
    from signals import generate_pullback_signals  # type: ignore

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
OUT_DIR = DATA_DIR / "experiments"

CONTRACTS: dict[str, ContractSpec] = {
    "MTX": ContractSpec(name="MTX", point_value=50.0, tick_size=1.0),
    "TX": ContractSpec(name="TX", point_value=200.0, tick_size=1.0),
    "TMF": ContractSpec(name="TMF", point_value=10.0, tick_size=1.0),
}


@dataclass
class TrailTrade:
    entry_time: pd.Timestamp
    entry_price: float
    direction: str
    exit_time: pd.Timestamp
    exit_price: float
    exit_reason: str
    gross_pnl_points: float
    gross_pnl_ntd: float
    total_cost_ntd: float
    net_pnl_ntd: float


def _load_bars(symbol: str) -> pd.DataFrame:
    raw = pd.read_csv(DATA_DIR / f"{symbol}_60min_real.csv")
    raw["datetime"] = pd.to_datetime(raw["datetime"])
    raw = raw.sort_values("datetime").set_index("datetime")
    return add_moving_averages(raw, fast=DEFAULT_STRATEGY.ma_fast, mid=DEFAULT_STRATEGY.ma_mid, slow=DEFAULT_STRATEGY.ma_slow)


def _apply_slippage(price: float, direction: str, *, is_entry: bool, slippage_points: float) -> float:
    if slippage_points == 0.0:
        return float(price)
    if direction == "long":
        return float(price + slippage_points) if is_entry else float(price - slippage_points)
    return float(price - slippage_points) if is_entry else float(price + slippage_points)


def run_trailing_backtest(
    df: pd.DataFrame,
    *,
    contract: ContractSpec,
    cost,
    stop_loss_points: float,
    take_profit_points: float | None,
    trailing: bool,
    slippage_points: float = 0.0,
) -> tuple[list[TrailTrade], dict]:
    """回測回測/回彈策略，支援固定停損/移動停損 + 可選停利上限。

    trailing=False: 停損固定不動（與現行引擎行為相同）。
    trailing=True : 停損只會朝有利方向移動（多單只上移、空單只下移），
                    用「前一根已收盤K棒」為止的最高/最低價決定，不偷看當前K棒走勢。
    take_profit_points=None: 不設停利上限，只靠停損（含移動停損）出場。
    """
    trades: list[TrailTrade] = []

    position: dict | None = None
    highest_high_since_entry = None  # 只用「已收盤」K棒更新
    lowest_low_since_entry = None

    n = len(df)
    for i in range(n):
        bar = df.iloc[i]
        bar_time = pd.Timestamp(df.index[i])

        # 進場（訊號於前一根K棒收盤確認，本根K棒開盤執行，與核心引擎一致）
        if position is None and i > 0:
            signal_row = df.iloc[i - 1]
            direction = signal_row.get("signal")
            if direction in ("long", "short"):
                entry_price = _apply_slippage(float(bar["open"]), direction, is_entry=True, slippage_points=slippage_points)
                if direction == "long":
                    sl = entry_price - stop_loss_points
                    tp = entry_price + take_profit_points if take_profit_points is not None else None
                else:
                    sl = entry_price + stop_loss_points
                    tp = entry_price - take_profit_points if take_profit_points is not None else None
                position = {
                    "entry_time": bar_time, "entry_price": entry_price, "direction": direction,
                    "sl": sl, "tp": tp,
                }
                highest_high_since_entry = float(bar["high"])
                lowest_low_since_entry = float(bar["low"])
                # 注意：不可 continue —— 正式引擎允許進場當根K棒立即檢查出場
                # （這正是之前發現的「同根K棒巴掉」現象的來源，此處必須保持一致）

        if position is not None:
            direction = position["direction"]

            # 先用「前一根已收盤」的最高/最低價移動停損，再評估本根K棒是否出場（避免偷看本根走勢）
            if trailing:
                if direction == "long" and highest_high_since_entry is not None:
                    candidate = highest_high_since_entry - stop_loss_points
                    position["sl"] = max(position["sl"], candidate)
                elif direction == "short" and lowest_low_since_entry is not None:
                    candidate = lowest_low_since_entry + stop_loss_points
                    position["sl"] = min(position["sl"], candidate)

            open_price = float(bar["open"])
            high_price = float(bar["high"])
            low_price = float(bar["low"])
            sl, tp = position["sl"], position["tp"]

            exit_event = None
            if direction == "long":
                if open_price <= sl:
                    exit_event = ("stop_loss", _apply_slippage(open_price, "long", is_entry=False, slippage_points=slippage_points))
                elif tp is not None and open_price >= tp:
                    exit_event = ("take_profit", _apply_slippage(open_price, "long", is_entry=False, slippage_points=slippage_points))
                elif low_price <= sl:
                    exit_event = ("stop_loss", _apply_slippage(sl, "long", is_entry=False, slippage_points=slippage_points))
                elif tp is not None and high_price >= tp:
                    exit_event = ("take_profit", _apply_slippage(tp, "long", is_entry=False, slippage_points=slippage_points))
            else:
                if open_price >= sl:
                    exit_event = ("stop_loss", _apply_slippage(open_price, "short", is_entry=False, slippage_points=slippage_points))
                elif tp is not None and open_price <= tp:
                    exit_event = ("take_profit", _apply_slippage(open_price, "short", is_entry=False, slippage_points=slippage_points))
                elif high_price >= sl:
                    exit_event = ("stop_loss", _apply_slippage(sl, "short", is_entry=False, slippage_points=slippage_points))
                elif tp is not None and low_price <= tp:
                    exit_event = ("take_profit", _apply_slippage(tp, "short", is_entry=False, slippage_points=slippage_points))

            if exit_event is not None:
                reason, exit_price = exit_event
                trades.append(_close_trade(position, bar_time, exit_price, reason, contract, cost))
                position = None
                highest_high_since_entry = None
                lowest_low_since_entry = None
            else:
                # 本根K棒收盤後，才把本根的高低價納入下一根的移動停損判斷依據
                highest_high_since_entry = max(highest_high_since_entry, high_price)
                lowest_low_since_entry = min(lowest_low_since_entry, low_price)

    if position is not None:
        last_time = pd.Timestamp(df.index[-1])
        last_close = float(df.iloc[-1]["close"])
        exit_price = _apply_slippage(last_close, position["direction"], is_entry=False, slippage_points=slippage_points)
        trades.append(_close_trade(position, last_time, exit_price, "end_of_data", contract, cost))

    summary = _build_summary(trades)
    return trades, summary


def _close_trade(position: dict, exit_time, exit_price: float, reason: str, contract: ContractSpec, cost) -> TrailTrade:
    direction_sign = 1.0 if position["direction"] == "long" else -1.0
    gross_pnl_points = (float(exit_price) - position["entry_price"]) * direction_sign
    gross_pnl_ntd = gross_pnl_points * float(contract.point_value)
    entry_tax = position["entry_price"] * float(contract.point_value) * float(cost.tax_rate)
    exit_tax = float(exit_price) * float(contract.point_value) * float(cost.tax_rate)
    total_cost_ntd = (2.0 * float(cost.commission_per_side)) + entry_tax + exit_tax
    net_pnl_ntd = gross_pnl_ntd - total_cost_ntd
    return TrailTrade(
        entry_time=position["entry_time"], entry_price=position["entry_price"], direction=position["direction"],
        exit_time=exit_time, exit_price=float(exit_price), exit_reason=reason,
        gross_pnl_points=float(gross_pnl_points), gross_pnl_ntd=float(gross_pnl_ntd),
        total_cost_ntd=float(total_cost_ntd), net_pnl_ntd=float(net_pnl_ntd),
    )


def _build_summary(trades: list[TrailTrade]) -> dict:
    n = len(trades)
    net_pnls = [t.net_pnl_ntd for t in trades]
    wins = [p for p in net_pnls if p > 0]
    losses = [p for p in net_pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    return {
        "total_trades": n,
        "win_rate": (len(wins) / n) if n else 0.0,
        "total_net_pnl_ntd": float(sum(net_pnls)),
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else float("inf"),
        "avg_win_ntd": (gross_profit / len(wins)) if wins else 0.0,
        "avg_loss_ntd": (sum(losses) / len(losses)) if losses else 0.0,
    }


VARIANTS = {
    "baseline (SL150/TP300 固定)": dict(stop_loss_points=150.0, take_profit_points=300.0, trailing=False),
    "wider_tp (SL150固定/TP600固定)": dict(stop_loss_points=150.0, take_profit_points=600.0, trailing=False),
    "trailing_capped (SL150移動/TP600上限)": dict(stop_loss_points=150.0, take_profit_points=600.0, trailing=True),
    "trailing_open (SL150移動/無TP上限)": dict(stop_loss_points=150.0, take_profit_points=None, trailing=True),
}


def main() -> None:
    results: dict[str, dict] = {}
    for symbol in ("MTX", "TX", "TMF"):
        bars = _load_bars(symbol)
        signaled = generate_pullback_signals(bars)
        contract = CONTRACTS[symbol]
        cost = cost_config_for_contract(symbol)
        results[symbol] = {}
        for variant_name, params in VARIANTS.items():
            trades, summary = run_trailing_backtest(signaled, contract=contract, cost=cost, **params)
            results[symbol][variant_name] = summary

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUT_DIR / "trailing_stop_ab_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)

    lines = ["# 移動停損 (Trailing Stop) A/B 測試結果\n",
             "回測/回彈策略，全樣本資料，4種SL/TP機制比較\n"]
    for symbol, variants in results.items():
        lines.append(f"\n## {symbol}\n")
        lines.append("| 變體 | 交易數 | 勝率 | 淨利(元) | 獲利因子 |")
        lines.append("|---|---|---|---|---|")
        for variant_name, s in variants.items():
            pf = s["profit_factor"]
            pf_str = f"{pf:.2f}" if pf != float("inf") else "∞"
            lines.append(f"| {variant_name} | {s['total_trades']} | {s['win_rate']*100:.1f}% | {s['total_net_pnl_ntd']:,.0f} | {pf_str} |")

    report = "\n".join(lines) + "\n"
    with open(OUT_DIR / "trailing_stop_ab_results.md", "w", encoding="utf-8") as f:
        f.write(report)

    print(report)


if __name__ == "__main__":
    main()
