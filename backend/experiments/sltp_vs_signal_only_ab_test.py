"""
sltp_vs_signal_only_ab_test.py — 比較「有停損停利點數」vs「拿掉停損停利、只靠訊號進出場」
兩種出場機制，分別套用在突破/跌破 (breakout) 與回測/回彈 (pullback) 兩套策略上。

背景（使用者需求原文）：
    突破/跌破：60K5MA 突破 60K20MA 之上 → 作多；跌破之下 → 作空。
               停損 = 進場反向 150 點，停利 = 進場正向 500 點。
    回測/回彈：多方在 60K60MA、60K20MA 之上時回測 60K20MA → 作多；
               空方在 60K60MA、60K20MA 之下時回彈 60K20MA → 作空。
               停損 = 進場反向 150 點，停利 = 進場正向 300 點。
    現在把停損停利點數拿掉，只等訊號出現才動作 —— 也就是進場後持倉到「下一個反向訊號」
    出現才出場（同時反手），比較「有點數」與「沒點數」兩種版本的績效差異。

變體定義：
    with_sltp     現有引擎預設行為（breakout: SL150/TP500；pullback: SL150/TP300 +
                  既有 immediate_reverse 機制）。維持與 backend/backtest_engine.py 100%
                  一致的既有規則，做為對照組。
    signal_only   完全拿掉 SL/TP 判斷；進場後只在「下一個反向訊號」出現時，於次一根
                  K棒開盤價出場並同時反手進場新方向。若持倉到資料結尾，則以最後一根
                  K棒收盤價（含滑價）平倉（end_of_data，與現有引擎規則一致）。

依專案慣例（README「重要回測原則」第 5 點與使用者指示）：一律使用全資料集回測，
不做 walk-forward / train-test 切分。

使用方式：
    python -m backend.experiments.sltp_vs_signal_only_ab_test
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Literal

import pandas as pd

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parents[1]))
    from backtest_engine import (  # type: ignore
        BacktestEngine,
        Trade,
        _OpenPosition,
        run_backtest,
        trades_to_dataframe,
    )
    from config import ContractSpec, DEFAULT_COST, DEFAULT_STRATEGY  # type: ignore
    from indicators import add_moving_averages  # type: ignore
    from signals import generate_breakout_signals, generate_pullback_signals  # type: ignore
    from experiments.proper_evaluation_with_dsr import (  # type: ignore
        _assumed_capital,
        _max_drawdown_pct,
        _sharpe_per_trade,
        _total_return,
        _trade_returns,
    )
else:  # pragma: no cover - exercised via package import/module execution
    from ..backtest_engine import BacktestEngine, Trade, _OpenPosition, run_backtest, trades_to_dataframe
    from ..config import ContractSpec, DEFAULT_COST, DEFAULT_STRATEGY
    from ..indicators import add_moving_averages
    from ..signals import generate_breakout_signals, generate_pullback_signals
    from .proper_evaluation_with_dsr import (
        _assumed_capital,
        _max_drawdown_pct,
        _sharpe_per_trade,
        _total_return,
        _trade_returns,
    )


Strategy = Literal["breakout", "pullback"]

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = DATA_DIR / "experiments"
OUTPUT_MD = OUTPUT_DIR / "sltp_vs_signal_only_ab_results.md"
OUTPUT_JSON = OUTPUT_DIR / "sltp_vs_signal_only_ab_results.json"

SYMBOLS = ("MTX", "TX")
STRATEGIES: tuple[Strategy, ...] = ("breakout", "pullback")

CONTRACT_SPECS: dict[str, ContractSpec] = {
    "MTX": ContractSpec(name="MTX", point_value=50.0, tick_size=1.0),
    "TX": ContractSpec(name="TX", point_value=200.0, tick_size=1.0),
    "TMF": ContractSpec(name="TMF", point_value=10.0, tick_size=1.0),
}

STRATEGY_LABEL = {"breakout": "突破/跌破 (Breakout)", "pullback": "回測/回彈 (Pullback)"}
SLTP_DESCRIPTION = {
    "breakout": "SL=150點 / TP=500點（既有預設）",
    "pullback": "SL=150點 / TP=300點 + opposite-arrow immediate_reverse（既有預設）",
}


def _load_bars(symbol: str) -> pd.DataFrame:
    frame = pd.read_csv(DATA_DIR / f"{symbol}_60min_real.csv", parse_dates=["datetime"], index_col="datetime")
    frame.index = pd.to_datetime(frame.index)
    return frame.sort_index()


def _prepare_enriched(symbol: str) -> pd.DataFrame:
    bars = _load_bars(symbol)
    enriched = add_moving_averages(
        bars,
        fast=DEFAULT_STRATEGY.ma_fast,
        mid=DEFAULT_STRATEGY.ma_mid,
        slow=DEFAULT_STRATEGY.ma_slow,
    )
    enriched.index = pd.to_datetime(enriched.index)
    return enriched.sort_index()


def _signaled_frame(strategy: Strategy, enriched: pd.DataFrame) -> pd.DataFrame:
    if strategy == "breakout":
        return generate_breakout_signals(enriched)
    return generate_pullback_signals(enriched)


def _run_with_sltp(signaled: pd.DataFrame, symbol: str, strategy: Strategy) -> tuple[list[Trade], dict[str, Any]]:
    """既有引擎預設行為：breakout 用固定 SL150/TP500；pullback 用固定 SL150/TP300 +
    immediate_reverse，兩者皆為 backend/backtest_engine.py 的既有規則，未做任何修改。"""
    return run_backtest(
        signaled,
        strategy=strategy,
        contract=CONTRACT_SPECS[symbol],
        cost=DEFAULT_COST,
        strategy_cfg=DEFAULT_STRATEGY,
        slippage_points=DEFAULT_COST.slippage_points,
    )


def _run_signal_only(
    signaled: pd.DataFrame, symbol: str, strategy: Strategy
) -> tuple[list[Trade], dict[str, Any]]:
    """拿掉 SL/TP：進場後持倉到「下一個反向訊號」才出場並反手，兩策略共用同一套邏輯。

    breakout 訊號本身已透過 `_apply_signal_columns(suppress_repeat_same_direction=True)`
    只在方向真正反轉時才發出新訊號，因此下一個訊號必然與目前持倉方向相反，等同於
    「只等訊號出現在操作」。pullback 訊號可能重複發出同方向訊號（同方向的新回測點），
    此時視為持倉不動，不重複進出場，只有方向真正相反時才出場反手。
    """
    contract = CONTRACT_SPECS[symbol]
    # 借用既有引擎的滑價/結算/彙總邏輯，但不使用其 SL/TP 判斷路徑。
    engine = BacktestEngine(
        strategy=strategy,
        contract=contract,
        cost=DEFAULT_COST,
        strategy_cfg=DEFAULT_STRATEGY,
        slippage_points=DEFAULT_COST.slippage_points,
    )
    prepared = signaled.copy()
    prepared.index = pd.to_datetime(prepared.index)
    prepared = prepared.sort_index()

    trades: list[Trade] = []
    position = None  # type: ignore[assignment]

    def _open(direction: str, raw_open: float, bar_time: pd.Timestamp):
        entry_price = engine._apply_slippage(float(raw_open), direction, is_entry=True)  # noqa: SLF001
        return _OpenPosition(
            entry_time=bar_time,
            entry_price=float(entry_price),
            direction=direction,
            stop_loss_price=float("nan"),
            take_profit_price=float("nan"),
        )

    for index in range(len(prepared)):
        bar = prepared.iloc[index]
        bar_time = pd.Timestamp(prepared.index[index])

        if index > 0:
            signal_row = prepared.iloc[index - 1]
            direction = signal_row.get("signal")
            if direction in ("long", "short"):
                current_open = float(bar["open"])
                if position is None:
                    position = _open(direction, current_open, bar_time)
                elif direction != position.direction:
                    exit_price = engine._apply_slippage(current_open, position.direction, is_entry=False)  # noqa: SLF001
                    trades.append(engine._close_trade(position, bar_time, exit_price, "immediate_reverse"))  # noqa: SLF001
                    position = _open(direction, current_open, bar_time)
                # direction == position.direction (可能發生於 pullback 重複同向觸碰)：
                # 視為持倉不動，不重複進出場。

    if position is not None:
        last_time = pd.Timestamp(prepared.index[-1])
        last_close = float(prepared.iloc[-1]["close"])
        exit_price = engine._apply_slippage(last_close, position.direction, is_entry=False)  # noqa: SLF001
        trades.append(engine._close_trade(position, last_time, exit_price, "end_of_data"))  # noqa: SLF001

    return trades, engine._build_summary(trades)  # noqa: SLF001


def _summarize(
    *, symbol: str, strategy: Strategy, variant_key: str, trades: list[Trade], summary: dict[str, Any],
    capital: float, years: float,
) -> dict[str, Any]:
    returns = _trade_returns(trades, capital)
    trades_per_year = len(trades) / years if years > 0 else 0.0
    sharpe, sharpe_flag = _sharpe_per_trade(returns, trades_per_year)
    profit_factor_raw = summary.get("profit_factor")
    profit_factor_display = "∞" if isinstance(profit_factor_raw, float) and profit_factor_raw == float("inf") else None
    profit_factor = None if profit_factor_display else float(profit_factor_raw or 0.0)

    return {
        "symbol": symbol,
        "strategy": strategy,
        "variant": variant_key,
        "n_trades": int(len(trades)),
        "win_rate": float(summary.get("win_rate", 0.0)),
        "profit_factor": profit_factor,
        "profit_factor_display": profit_factor_display,
        "total_net_pnl_ntd": float(summary.get("total_net_pnl_ntd", 0.0)),
        "max_drawdown_ntd": float(summary.get("max_drawdown_ntd", 0.0)),
        "total_return_pct": _total_return(returns) * 100.0,
        "max_drawdown_pct": _max_drawdown_pct(returns) * 100.0,
        "sharpe_ratio": sharpe,
        "sharpe_flag": sharpe_flag,
        "avg_win_ntd": float(summary.get("avg_win_ntd", 0.0)),
        "avg_loss_ntd": float(summary.get("avg_loss_ntd", 0.0)),
    }


def _fmt_num(value: float | int | None, digits: int = 0) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):,.{digits}f}"


def _fmt_pct(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):.{digits}f}%"


def _fmt_pf(record: dict[str, Any]) -> str:
    if record["profit_factor_display"] is not None:
        return record["profit_factor_display"]
    return _fmt_num(record["profit_factor"], 2)


def _fmt_sharpe(record: dict[str, Any]) -> str:
    if record["sharpe_ratio"] is not None:
        return f"{record['sharpe_ratio']:.2f}"
    if record["sharpe_flag"] == "unstable_near_zero_variance":
        return "不穩定(近零變異)"
    if record["sharpe_flag"] == "insufficient_trades":
        return "N/A(交易太少)"
    return "N/A"


def run() -> dict[str, Any]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []

    for symbol in SYMBOLS:
        enriched = _prepare_enriched(symbol)
        contract = CONTRACT_SPECS[symbol]
        capital = _assumed_capital(enriched, contract)
        span_days = (enriched.index[-1] - enriched.index[0]).total_seconds() / 86400.0
        years = max(span_days / 365.25, 1e-6)

        for strategy in STRATEGIES:
            signaled = _signaled_frame(strategy, enriched)

            sltp_trades, sltp_summary = _run_with_sltp(signaled, symbol, strategy)
            records.append(
                _summarize(
                    symbol=symbol, strategy=strategy, variant_key="with_sltp",
                    trades=sltp_trades, summary=sltp_summary, capital=capital, years=years,
                )
            )

            signal_only_trades, signal_only_summary = _run_signal_only(signaled, symbol, strategy)
            records.append(
                _summarize(
                    symbol=symbol, strategy=strategy, variant_key="signal_only",
                    trades=signal_only_trades, summary=signal_only_summary, capital=capital, years=years,
                )
            )

            trades_to_dataframe(sltp_trades).to_csv(
                OUTPUT_DIR / f"sltp_vs_signal_only_{symbol}_{strategy}_with_sltp_trades.csv", index=False
            )
            trades_to_dataframe(signal_only_trades).to_csv(
                OUTPUT_DIR / f"sltp_vs_signal_only_{symbol}_{strategy}_signal_only_trades.csv", index=False
            )

    return {"records": records, "data_span": {s: str(_load_bars(s).index[[0, -1]].tolist()) for s in SYMBOLS}}


def _combined_by_strategy_variant(records: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """加總 MTX+TX 兩商品的淨損益/交易數，供整體對照使用（各商品點值不同，僅加總金額與交易數，
    不加總點數/百分比類指標）。"""
    combined: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        key = (record["strategy"], record["variant"])
        bucket = combined.setdefault(
            key, {"n_trades": 0, "total_net_pnl_ntd": 0.0, "wins": 0.0, "losses": 0.0}
        )
        bucket["n_trades"] += record["n_trades"]
        bucket["total_net_pnl_ntd"] += record["total_net_pnl_ntd"]
    return combined


def write_report(result: dict[str, Any]) -> None:
    records = result["records"]
    lines: list[str] = []
    lines.append("# 有停損停利點數 vs 拿掉點數只靠訊號進出場 —— A/B 對照報告\n")
    lines.append(
        "本報告比較兩套策略（突破/跌破、回測/回彈）在「維持既有固定 SL/TP」與"
        "「拿掉 SL/TP、只在下一個反向訊號出現時出場反手」兩種出場機制下的績效差異。"
        "資料採全資料集回測（不做 walk-forward / train-test 切分）。\n"
    )

    for strategy in STRATEGIES:
        lines.append(f"## {STRATEGY_LABEL[strategy]}\n")
        lines.append(f"- with_sltp 規則：{SLTP_DESCRIPTION[strategy]}")
        lines.append("- signal_only 規則：無 SL/TP，持倉到下一個反向訊號出現才於次根開盤出場並反手\n")
        lines.append(
            "| 商品 | 版本 | 交易數 | 勝率 | Profit Factor | 總淨損益(TWD) | 總報酬率 | 最大回落 | Sharpe(年化) |"
        )
        lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
        for symbol in SYMBOLS:
            for variant_key, variant_label in (("with_sltp", "有點數"), ("signal_only", "無點數(純訊號)")):
                record = next(
                    r for r in records
                    if r["symbol"] == symbol and r["strategy"] == strategy and r["variant"] == variant_key
                )
                lines.append(
                    "| {symbol} | {label} | {n} | {wr} | {pf} | {pnl} | {ret} | {dd} | {sharpe} |".format(
                        symbol=symbol,
                        label=variant_label,
                        n=record["n_trades"],
                        wr=_fmt_pct(record["win_rate"] * 100.0),
                        pf=_fmt_pf(record),
                        pnl=_fmt_num(record["total_net_pnl_ntd"]),
                        ret=_fmt_pct(record["total_return_pct"]),
                        dd=_fmt_pct(record["max_drawdown_pct"]),
                        sharpe=_fmt_sharpe(record),
                    )
                )
        lines.append("")

    combined = _combined_by_strategy_variant(records)
    lines.append("## 合計（MTX + TX 淨損益加總，僅供整體方向參考）\n")
    lines.append("| 策略 | 版本 | 總交易數 | 總淨損益(TWD) |")
    lines.append("|---|---|---:|---:|")
    for strategy in STRATEGIES:
        for variant_key, variant_label in (("with_sltp", "有點數"), ("signal_only", "無點數(純訊號)")):
            bucket = combined[(strategy, variant_key)]
            lines.append(
                f"| {STRATEGY_LABEL[strategy]} | {variant_label} | {bucket['n_trades']} | "
                f"{_fmt_num(bucket['total_net_pnl_ntd'])} |"
            )
    lines.append("")

    lines.append("## 結論摘要\n")
    for strategy in STRATEGIES:
        sltp_pnl = combined[(strategy, "with_sltp")]["total_net_pnl_ntd"]
        signal_pnl = combined[(strategy, "signal_only")]["total_net_pnl_ntd"]
        delta = signal_pnl - sltp_pnl
        better = "無點數(純訊號)較佳" if signal_pnl > sltp_pnl else ("有點數較佳" if sltp_pnl > signal_pnl else "兩者持平")
        lines.append(
            f"- **{STRATEGY_LABEL[strategy]}**：有點數合計淨損益 {_fmt_num(sltp_pnl)} TWD，"
            f"無點數合計淨損益 {_fmt_num(signal_pnl)} TWD，差異 {_fmt_num(delta)} TWD → {better}。"
        )
    lines.append(
        "\n⚠️ 樣本僅涵蓋本專案內建的 MTX/TX 60 分鐘資料區間，交易筆數有限，"
        "結論僅供方向性參考，實盤仍應留意風控與樣本外驗證。"
    )

    OUTPUT_MD.write_text("\n".join(lines), encoding="utf-8")

    with OUTPUT_JSON.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)


def main() -> None:
    result = run()
    write_report(result)
    print(f"報告已輸出：{OUTPUT_MD}")
    print(f"原始資料已輸出：{OUTPUT_JSON}")


if __name__ == "__main__":
    main()
