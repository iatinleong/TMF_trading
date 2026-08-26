from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parents[1]))
    from backtest_engine import BacktestEngine, Trade, run_backtest, trades_to_dataframe  # type: ignore
    from config import ContractSpec, DEFAULT_COST, DEFAULT_STRATEGY  # type: ignore
    from indicators import add_moving_averages  # type: ignore
    from signals import generate_breakout_signals, generate_pullback_signals  # type: ignore
    from experiments.proper_evaluation_with_dsr import (  # type: ignore
        _assumed_capital,
        _atr14,
        _max_drawdown_pct,
        _sharpe_per_trade,
        _total_return,
        _trade_returns,
        evaluate_all as evaluate_breakout_dsr,
    )
else:  # pragma: no cover - exercised via package import/module execution
    from ..backtest_engine import BacktestEngine, Trade, run_backtest, trades_to_dataframe
    from ..config import ContractSpec, DEFAULT_COST, DEFAULT_STRATEGY
    from ..indicators import add_moving_averages
    from ..signals import generate_breakout_signals, generate_pullback_signals
    from .proper_evaluation_with_dsr import (
        _assumed_capital,
        _atr14,
        _max_drawdown_pct,
        _sharpe_per_trade,
        _total_return,
        _trade_returns,
        evaluate_all as evaluate_breakout_dsr,
    )


Direction = Literal["long", "short"]

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = DATA_DIR / "experiments"
OUTPUT_MD = OUTPUT_DIR / "pullback_strategy_ab_results.md"
OUTPUT_JSON = OUTPUT_DIR / "pullback_strategy_ab_results.json"

SYMBOLS = ("MTX", "TX")
PRIOR_BREAKOUT_VARIANT_TRIALS = 12

CONTRACT_SPECS: dict[str, ContractSpec] = {
    "MTX": ContractSpec(name="MTX", point_value=50.0, tick_size=1.0),
    "TX": ContractSpec(name="TX", point_value=200.0, tick_size=1.0),
}


@dataclass(frozen=True)
class VariantSpec:
    key: str
    label: str
    description: str
    stop_loss_points: float | None = None
    atr_multiple: float | None = None
    disable_immediate_reverse: bool = False


@dataclass
class VariablePosition:
    entry_time: pd.Timestamp
    entry_price: float
    direction: Direction
    stop_loss_price: float
    take_profit_price: float


VARIANTS: list[VariantSpec] = [
    VariantSpec(
        key="baseline",
        label="A. Baseline",
        description="原始 pullback：SL=150、TP=300，保留 opposite-arrow immediate_reverse。",
        stop_loss_points=DEFAULT_STRATEGY.pullback_stop_loss_points,
    ),
    VariantSpec(
        key="tighter_sl_105",
        label="B1. Tight SL -30%",
        description="固定停損縮至 105 點（較 baseline 150 點縮小 30%），TP 維持 300 點。",
        stop_loss_points=DEFAULT_STRATEGY.pullback_stop_loss_points * 0.7,
    ),
    VariantSpec(
        key="tighter_sl_75",
        label="B2. Tight SL -50%",
        description="固定停損縮至 75 點（較 baseline 150 點縮小 50%），TP 維持 300 點。",
        stop_loss_points=DEFAULT_STRATEGY.pullback_stop_loss_points * 0.5,
    ),
    VariantSpec(
        key="wider_sl_225",
        label="C1. Wide SL +50%",
        description="固定停損放寬至 225 點（較 baseline 增加 50%），TP 維持 300 點。",
        stop_loss_points=DEFAULT_STRATEGY.pullback_stop_loss_points * 1.5,
    ),
    VariantSpec(
        key="wider_sl_300",
        label="C2. Wide SL +100%",
        description="固定停損放寬至 300 點（較 baseline 增加 100%），TP 維持 300 點。",
        stop_loss_points=DEFAULT_STRATEGY.pullback_stop_loss_points * 2.0,
    ),
    VariantSpec(
        key="atr14_sl_1_5x",
        label="D1. ATR14 × 1.5 SL",
        description="訊號棒 ATR14 × 1.5 作為停損距離，TP 維持 baseline 300 點。",
        atr_multiple=1.5,
    ),
    VariantSpec(
        key="atr14_sl_2_5x",
        label="D2. ATR14 × 2.5 SL",
        description="訊號棒 ATR14 × 2.5 作為停損距離，TP 維持 baseline 300 點。",
        atr_multiple=2.5,
    ),
    VariantSpec(
        key="no_immediate_reverse",
        label="E. Disable immediate_reverse",
        description="停用 opposite-arrow 即時平倉反手；持倉改為只靠自身 SL/TP 出場。",
        stop_loss_points=DEFAULT_STRATEGY.pullback_stop_loss_points,
        disable_immediate_reverse=True,
    ),
]


def _load_bars(symbol: str) -> pd.DataFrame:
    frame = pd.read_csv(DATA_DIR / f"{symbol}_60min_real.csv", parse_dates=["datetime"], index_col="datetime")
    frame.index = pd.to_datetime(frame.index)
    return frame.sort_index()


def _prepare_pullback_frame(symbol: str) -> pd.DataFrame:
    bars = _load_bars(symbol)
    enriched = add_moving_averages(
        bars,
        fast=DEFAULT_STRATEGY.ma_fast,
        mid=DEFAULT_STRATEGY.ma_mid,
        slow=DEFAULT_STRATEGY.ma_slow,
    )
    enriched["atr_14"] = _atr14(enriched)
    enriched.index = pd.to_datetime(enriched.index)
    return enriched.sort_index()


def _pullback_signal_masks(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    previous_low = df["low"].shift(1)
    previous_high = df["high"].shift(1)
    previous_mid = df["ma_mid"].shift(1)

    bullish_regime = df["close"].gt(df["ma_slow"]) & df["close"].ge(df["ma_mid"])
    bearish_regime = df["close"].lt(df["ma_slow"]) & df["close"].le(df["ma_mid"])

    long_mask = (
        bullish_regime
        & previous_low.gt(previous_mid)
        & df["low"].le(df["ma_mid"])
        & df["ma_mid"].le(df["close"])
    )
    short_mask = (
        bearish_regime
        & previous_high.lt(previous_mid)
        & df["high"].ge(df["ma_mid"])
        & df["ma_mid"].ge(df["close"])
    )
    return long_mask.fillna(False), short_mask.fillna(False)


def _generate_pullback_signals_without_immediate_reverse(df: pd.DataFrame) -> pd.DataFrame:
    long_mask, short_mask = _pullback_signal_masks(df)
    if (long_mask & short_mask).any():
        raise ValueError("A bar cannot be both a long and short pullback signal.")

    signaled = df.copy()
    signals = np.where(long_mask, "long", np.where(short_mask, "short", None))
    colors = np.where(
        signals == "long",
        "red",
        np.where(signals == "short", "green", None),
    )
    signaled["signal"] = pd.Series(signals, index=signaled.index, dtype="object")
    signaled["signal_color"] = pd.Series(colors, index=signaled.index, dtype="object")
    signaled["signal_bar_close_time"] = pd.Series(pd.to_datetime(signaled.index), index=signaled.index, dtype="datetime64[ns]")
    signaled["immediate_reverse"] = False
    return signaled


def _build_signal_frame(enriched: pd.DataFrame, spec: VariantSpec) -> pd.DataFrame:
    if spec.disable_immediate_reverse:
        return _generate_pullback_signals_without_immediate_reverse(enriched)
    return generate_pullback_signals(enriched)


def _safe_float(value: float | int | None) -> float | None:
    if value is None:
        return None
    value = float(value)
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def _fmt_num(value: float | int | None, digits: int = 0) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):,.{digits}f}"


def _fmt_pct(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):.{digits}f}%"


def _fmt_pf(value: float | None, display: str | None) -> str:
    if display is not None:
        return display
    return _fmt_num(value, 2)


def _fmt_sharpe(value: float | None, flag: str | None) -> str:
    if value is not None:
        return f"{value:.2f}"
    if flag == "unstable_near_zero_variance":
        return "不穩定(近零變異)"
    if flag == "insufficient_trades":
        return "N/A(交易太少)"
    return "N/A"


def _profit_factor_value(summary: dict[str, Any]) -> tuple[float | None, str | None]:
    raw_value = summary.get("profit_factor")
    if isinstance(raw_value, (float, int)) and math.isinf(float(raw_value)):
        return None, "∞"
    return _safe_float(raw_value), None


def _run_fixed_variant(signaled: pd.DataFrame, symbol: str, spec: VariantSpec) -> tuple[list[Trade], dict[str, Any]]:
    strategy_cfg = replace(
        DEFAULT_STRATEGY,
        pullback_stop_loss_points=float(spec.stop_loss_points),
        pullback_take_profit_points=float(DEFAULT_STRATEGY.pullback_take_profit_points),
    )
    return run_backtest(
        signaled,
        strategy="pullback",
        contract=CONTRACT_SPECS[symbol],
        cost=DEFAULT_COST,
        strategy_cfg=strategy_cfg,
        slippage_points=DEFAULT_COST.slippage_points,
    )


def _open_variable_position(
    *,
    engine: BacktestEngine,
    bar_time: pd.Timestamp,
    raw_open: float,
    direction: Direction,
    stop_points: float,
    take_profit_points: float,
) -> VariablePosition:
    entry_price = engine._apply_slippage(raw_open, direction, is_entry=True)
    if direction == "long":
        stop_loss_price = entry_price - stop_points
        take_profit_price = entry_price + take_profit_points
    else:
        stop_loss_price = entry_price + stop_points
        take_profit_price = entry_price - take_profit_points
    return VariablePosition(
        entry_time=bar_time,
        entry_price=float(entry_price),
        direction=direction,
        stop_loss_price=float(stop_loss_price),
        take_profit_price=float(take_profit_price),
    )


def _run_atr_variant(signaled: pd.DataFrame, symbol: str, spec: VariantSpec) -> tuple[list[Trade], dict[str, Any]]:
    engine = BacktestEngine(
        strategy="pullback",
        contract=CONTRACT_SPECS[symbol],
        cost=DEFAULT_COST,
        strategy_cfg=DEFAULT_STRATEGY,
        slippage_points=DEFAULT_COST.slippage_points,
    )
    trades: list[Trade] = []
    position: VariablePosition | None = None
    prepared = signaled.copy().sort_index()
    take_profit_points = float(DEFAULT_STRATEGY.pullback_take_profit_points)

    for index in range(len(prepared)):
        bar = prepared.iloc[index]
        bar_time = pd.Timestamp(prepared.index[index])

        if index > 0:
            signal_row = prepared.iloc[index - 1]
            direction = signal_row.get("signal")
            atr_value = signal_row.get("atr_14")
            current_open = float(bar["open"])

            if position is not None and direction in {"long", "short"} and bool(signal_row.get("immediate_reverse", False)):
                reverse_direction = direction
                trades.append(
                    engine._close_trade(
                        position,
                        bar_time,
                        engine._apply_slippage(current_open, reverse_direction, is_entry=True),
                        "immediate_reverse",
                    )
                )
                position = None
                if pd.notna(atr_value) and float(atr_value) > 0:
                    position = _open_variable_position(
                        engine=engine,
                        bar_time=bar_time,
                        raw_open=current_open,
                        direction=reverse_direction,
                        stop_points=float(atr_value) * float(spec.atr_multiple),
                        take_profit_points=take_profit_points,
                    )

            if position is None and direction in {"long", "short"} and pd.notna(atr_value) and float(atr_value) > 0:
                position = _open_variable_position(
                    engine=engine,
                    bar_time=bar_time,
                    raw_open=current_open,
                    direction=direction,
                    stop_points=float(atr_value) * float(spec.atr_multiple),
                    take_profit_points=take_profit_points,
                )

        if position is not None:
            exit_event = engine._evaluate_exit_on_bar(position, bar)
            if exit_event is not None:
                trades.append(engine._close_trade(position, bar_time, exit_event["exit_price"], exit_event["reason"]))
                position = None

    if position is not None:
        last_time = pd.Timestamp(prepared.index[-1])
        last_close = float(prepared.iloc[-1]["close"])
        trades.append(
            engine._close_trade(
                position,
                last_time,
                engine._apply_slippage(last_close, position.direction, is_entry=False),
                "end_of_data",
            )
        )

    return trades, engine._build_summary(trades)


def _summarize_variant(
    *,
    symbol: str,
    spec: VariantSpec,
    trades: list[Trade],
    summary: dict[str, Any],
    capital: float,
    years: float,
) -> dict[str, Any]:
    returns = _trade_returns(trades, capital)
    trades_per_year = len(trades) / years if years > 0 else 0.0
    sharpe, sharpe_flag = _sharpe_per_trade(returns, trades_per_year)
    profit_factor, profit_factor_display = _profit_factor_value(summary)
    trade_frame = trades_to_dataframe(trades)

    exit_reason_counts: dict[str, int] = {}
    if not trade_frame.empty:
        exit_reason_counts = {str(key): int(value) for key, value in trade_frame["exit_reason"].value_counts().to_dict().items()}

    return {
        "variant_key": spec.key,
        "variant_label": spec.label,
        "description": spec.description,
        "symbol": symbol,
        "stop_loss_points": _safe_float(spec.stop_loss_points),
        "take_profit_points": float(DEFAULT_STRATEGY.pullback_take_profit_points),
        "atr_multiple": _safe_float(spec.atr_multiple),
        "disable_immediate_reverse": spec.disable_immediate_reverse,
        "n_trades": int(len(trades)),
        "win_rate": float(summary.get("win_rate", 0.0)),
        "profit_factor": profit_factor,
        "profit_factor_display": profit_factor_display,
        "total_net_pnl_ntd": float(summary.get("total_net_pnl_ntd", 0.0)),
        "total_return_pct": _total_return(returns) * 100.0,
        "sharpe_ratio": sharpe,
        "sharpe_flag": sharpe_flag,
        "max_drawdown_pct": _max_drawdown_pct(returns) * 100.0,
        "trades_per_year_extrapolated": float(trades_per_year),
        "exit_reason_counts": exit_reason_counts,
    }


def _collect_breakout_baseline(symbol: str, bars: pd.DataFrame) -> dict[str, Any]:
    enriched = add_moving_averages(
        bars,
        fast=DEFAULT_STRATEGY.ma_fast,
        mid=DEFAULT_STRATEGY.ma_mid,
        slow=DEFAULT_STRATEGY.ma_slow,
    )
    signaled = generate_breakout_signals(enriched)
    trades, summary = run_backtest(
        signaled,
        strategy="breakout",
        contract=CONTRACT_SPECS[symbol],
        cost=DEFAULT_COST,
        strategy_cfg=DEFAULT_STRATEGY,
        slippage_points=DEFAULT_COST.slippage_points,
    )
    capital = _assumed_capital(bars, CONTRACT_SPECS[symbol])
    span_days = (bars.index[-1] - bars.index[0]).total_seconds() / 86400.0
    years = max(span_days / 365.25, 1e-6)
    returns = _trade_returns(trades, capital)
    trades_per_year = len(trades) / years if years > 0 else 0.0
    sharpe, sharpe_flag = _sharpe_per_trade(returns, trades_per_year)
    return {
        "n_trades": int(len(trades)),
        "win_rate": float(summary.get("win_rate", 0.0)),
        "profit_factor": _safe_float(summary.get("profit_factor")),
        "total_net_pnl_ntd": float(summary.get("total_net_pnl_ntd", 0.0)),
        "total_return_pct": _total_return(returns) * 100.0,
        "sharpe_ratio": sharpe,
        "sharpe_flag": sharpe_flag,
        "max_drawdown_pct": _max_drawdown_pct(returns) * 100.0,
    }


def _compute_multiple_testing_update(results_by_symbol: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    breakout_results = evaluate_breakout_dsr()
    breakout_nonbaseline_sharpes: list[float] = []
    for family in ("entry_filters", "exit_mechanisms"):
        for symbol_results in breakout_results[family].values():
            for key, metrics in symbol_results.items():
                value = metrics.get("sharpe_per_trade_annualized")
                if key != "baseline" and value is not None:
                    breakout_nonbaseline_sharpes.append(float(value))

    pullback_nonbaseline_sharpes: list[float] = []
    for symbol_results in results_by_symbol.values():
        for item in symbol_results:
            if item["variant_key"] != "baseline" and item["sharpe_ratio"] is not None:
                pullback_nonbaseline_sharpes.append(float(item["sharpe_ratio"]))

    combined_sharpes = breakout_nonbaseline_sharpes + pullback_nonbaseline_sharpes
    pullback_new_trials = len(VARIANTS) - 1
    combined_trial_count = PRIOR_BREAKOUT_VARIANT_TRIALS + pullback_new_trials
    sigma_combined = float(np.std(combined_sharpes, ddof=1)) if len(combined_sharpes) > 1 else 0.0
    updated_expected_max_sharpe = sigma_combined * math.sqrt(2 * math.log(max(combined_trial_count, 2)))
    prior_sigma = float(breakout_results["multiple_testing_haircut"]["sigma_of_observed_sharpes"])
    prior_threshold = float(breakout_results["multiple_testing_haircut"]["expected_max_sharpe_from_pure_luck"])
    threshold_with_prior_sigma_only = prior_sigma * math.sqrt(2 * math.log(max(combined_trial_count, 2)))

    return {
        "prior_breakout_variant_trials": PRIOR_BREAKOUT_VARIANT_TRIALS,
        "new_pullback_variant_trials": pullback_new_trials,
        "combined_variant_trials": combined_trial_count,
        "prior_breakout_sigma_sr": prior_sigma,
        "prior_breakout_luck_threshold": prior_threshold,
        "updated_luck_threshold_if_only_N_grows": threshold_with_prior_sigma_only,
        "combined_sigma_sr": sigma_combined,
        "updated_luck_threshold_combined_sigma": updated_expected_max_sharpe,
        "note": (
            "先前 breakout 研究已累積 12 個非 baseline 變體；本次 pullback 新增 "
            f"{pullback_new_trials} 個非 baseline 變體後，跨整個專案的試驗次數提高到 "
            f"{combined_trial_count}。"
        ),
    }


def _run_all_variants() -> dict[str, Any]:
    results_by_symbol: dict[str, list[dict[str, Any]]] = {}
    baseline_breakout_comparison: dict[str, dict[str, Any]] = {}
    date_ranges: dict[str, dict[str, Any]] = {}

    for symbol in SYMBOLS:
        bars = _load_bars(symbol)
        enriched = _prepare_pullback_frame(symbol)
        capital = _assumed_capital(bars, CONTRACT_SPECS[symbol])
        span_days = (bars.index[-1] - bars.index[0]).total_seconds() / 86400.0
        years = max(span_days / 365.25, 1e-6)

        date_ranges[symbol] = {
            "start": pd.Timestamp(bars.index.min()).isoformat(),
            "end": pd.Timestamp(bars.index.max()).isoformat(),
            "bar_count": int(len(bars)),
        }

        metrics_list: list[dict[str, Any]] = []
        for spec in VARIANTS:
            signaled = _build_signal_frame(enriched, spec)
            if spec.atr_multiple is None:
                trades, summary = _run_fixed_variant(signaled, symbol, spec)
            else:
                trades, summary = _run_atr_variant(signaled, symbol, spec)
            metrics_list.append(
                _summarize_variant(
                    symbol=symbol,
                    spec=spec,
                    trades=trades,
                    summary=summary,
                    capital=capital,
                    years=years,
                )
            )

        results_by_symbol[symbol] = metrics_list
        baseline_breakout_comparison[symbol] = _collect_breakout_baseline(symbol, bars)

    multiple_testing = _compute_multiple_testing_update(results_by_symbol)
    return {
        "metadata": {
            "strategy": "pullback",
            "symbols": list(SYMBOLS),
            "date_ranges": date_ranges,
            "baseline_config": {
                "ma_fast": DEFAULT_STRATEGY.ma_fast,
                "ma_mid": DEFAULT_STRATEGY.ma_mid,
                "ma_slow": DEFAULT_STRATEGY.ma_slow,
                "pullback_stop_loss_points": DEFAULT_STRATEGY.pullback_stop_loss_points,
                "pullback_take_profit_points": DEFAULT_STRATEGY.pullback_take_profit_points,
            },
            "methodology_note": (
                "所有變體都在訊號產生/進場前就改變規則，不做事後 retroactive slicing。"
                "no_immediate_reverse 版本不是單純把欄位改成 False，而是改成『每個合格回測/回彈 setup 都可發訊號，"
                "但 opposite arrow 不再強迫平倉反手』，避免 ignored opposite signal 仍污染 alternating-state。"
            ),
        },
        "results_by_symbol": results_by_symbol,
        "breakout_baseline_comparison": baseline_breakout_comparison,
        "multiple_testing_update": multiple_testing,
    }


def _table_for_symbol(metrics_list: list[dict[str, Any]]) -> str:
    lines = [
        "| 變體 | 交易數 | 勝率 | Profit Factor | 總淨損益(NTD) | Total Return (%) | Sharpe Ratio | Max DD (%) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in metrics_list:
        lines.append(
            "| "
            + " | ".join(
                [
                    item["variant_label"],
                    str(item["n_trades"]),
                    _fmt_pct(item["win_rate"] * 100.0, 1),
                    _fmt_pf(item["profit_factor"], item["profit_factor_display"]),
                    _fmt_num(item["total_net_pnl_ntd"], 0),
                    _fmt_num(item["total_return_pct"], 2),
                    _fmt_sharpe(item["sharpe_ratio"], item["sharpe_flag"]),
                    _fmt_num(item["max_drawdown_pct"], 2),
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def _find_variant(metrics_list: list[dict[str, Any]], key: str) -> dict[str, Any]:
    for item in metrics_list:
        if item["variant_key"] == key:
            return item
    raise KeyError(key)


def _best_variants(metrics_list: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    def sort_key(item: dict[str, Any]) -> tuple[float, float, float]:
        sharpe = item["sharpe_ratio"] if item["sharpe_ratio"] is not None else -9999.0
        profit_factor = item["profit_factor"] if item["profit_factor"] is not None else -9999.0
        return (item["total_return_pct"], sharpe, profit_factor)

    ordered = sorted(metrics_list, key=sort_key)
    return ordered[-1], ordered[0]


def _comparison_lines(results: dict[str, Any]) -> list[str]:
    lines: list[str] = [
        "## 重點觀察",
        "",
    ]
    for symbol in SYMBOLS:
        metrics_list = results["results_by_symbol"][symbol]
        baseline = _find_variant(metrics_list, "baseline")
        no_reverse = _find_variant(metrics_list, "no_immediate_reverse")
        best, worst = _best_variants(metrics_list)
        breakout = results["breakout_baseline_comparison"][symbol]
        lines.extend(
            [
                f"### {symbol}",
                f"- **pullback 最佳 Total Return 變體**：{best['variant_label']}，Total Return {_fmt_num(best['total_return_pct'], 2)}%、Sharpe {_fmt_sharpe(best['sharpe_ratio'], best['sharpe_flag'])}、總淨損益 {_fmt_num(best['total_net_pnl_ntd'], 0)} NTD。",
                f"- **pullback 最差變體**：{worst['variant_label']}，Total Return {_fmt_num(worst['total_return_pct'], 2)}%、Sharpe {_fmt_sharpe(worst['sharpe_ratio'], worst['sharpe_flag'])}。",
                f"- **停用 immediate_reverse 的影響**：baseline {baseline['n_trades']} 筆 / {_fmt_num(baseline['total_return_pct'], 2)}%，停用後 {no_reverse['n_trades']} 筆 / {_fmt_num(no_reverse['total_return_pct'], 2)}%；baseline immediate_reverse 出場次數 = {baseline['exit_reason_counts'].get('immediate_reverse', 0)}，表示改善主因不是『少了被強迫反手的實際出場』，而是 baseline 為了支援 reverse 所採用的 alternating-only signal contract 壓掉了許多同向 pullback setup。",
                f"- **pullback baseline vs breakout baseline**：pullback baseline 勝率 {_fmt_pct(baseline['win_rate'] * 100.0, 1)}、Total Return {_fmt_num(baseline['total_return_pct'], 2)}%、Sharpe {_fmt_sharpe(baseline['sharpe_ratio'], baseline['sharpe_flag'])}；breakout baseline 勝率 {_fmt_pct(breakout['win_rate'] * 100.0, 1)}、Total Return {_fmt_num(breakout['total_return_pct'], 2)}%、Sharpe {_fmt_sharpe(breakout['sharpe_ratio'], breakout['sharpe_flag'])}。",
                "",
            ]
        )
    return lines


def _build_markdown(results: dict[str, Any]) -> str:
    mt_update = results["multiple_testing_update"]
    lines = [
        "# Pullback 策略 A/B 測試結果",
        "",
        "## 實驗設定",
        f"- 商品：{', '.join(SYMBOLS)}",
        f"- pullback baseline：60MA + 20MA 趨勢對齊，回測/回彈 20MA 進場，SL={DEFAULT_STRATEGY.pullback_stop_loss_points:.0f} 點，TP={DEFAULT_STRATEGY.pullback_take_profit_points:.0f} 點。",
        "- 成交規則：沿用既有回測引擎的下一根開盤進場、同棒先停損、跳空以開盤價成交、含雙邊成本與稅。",
        "- ATR 變體：訊號棒收盤時計算 ATR14，下一根開盤進場，停損距離 = ATR14 × 倍數，TP 維持 300 點。",
        "- `Disable immediate_reverse` 變體：保留同一組 pullback regime / 20MA 觸價邏輯，但 opposite arrow 不再即時平倉反手；這個版本改以『每個合格 setup 都可發訊號』重建 signal frame，避免單純把 flag 清掉後仍被 alternating-state 汙染。",
        "",
        "## 變體清單",
        "",
    ]

    for spec in VARIANTS:
        lines.append(f"- **{spec.label}**：{spec.description}")

    lines.extend(["", "## 各商品結果", ""])

    for symbol in SYMBOLS:
        date_info = results["metadata"]["date_ranges"][symbol]
        lines.extend(
            [
                f"### {symbol}",
                f"- 資料區間：{date_info['start']} ~ {date_info['end']}（{date_info['bar_count']} 根 60 分 K）",
                "",
                _table_for_symbol(results["results_by_symbol"][symbol]),
                "",
            ]
        )

    lines.extend(_comparison_lines(results))
    lines.extend(
        [
            "## 多重檢定 / Deflated Sharpe Ratio 脈絡",
            "",
            f"- 先前 breakout 研究已累積 **{mt_update['prior_breakout_variant_trials']}** 個非 baseline 變體，純運氣 Sharpe 門檻約 **{mt_update['prior_breakout_luck_threshold']:.2f}**（見 `proper_evaluation_with_dsr.md`）。",
            f"- 本次 pullback 又新增 **{mt_update['new_pullback_variant_trials']}** 個非 baseline 變體，因此整個專案的累積試驗次數已變成 **{mt_update['combined_variant_trials']} = 12 + {mt_update['new_pullback_variant_trials']}**。",
            f"- 若先只固定沿用舊 breakout 的 sigma_SR={mt_update['prior_breakout_sigma_sr']:.3f}，單純把 N 從 12 擴到 {mt_update['combined_variant_trials']}，則『純運氣可抽到的最高 Sharpe』門檻大約會升到 **{mt_update['updated_luck_threshold_if_only_N_grows']:.2f}**。",
            f"- 若把本次 pullback 非 baseline 變體也一起納入樣本內 Sharpe 離散度估計，combined sigma_SR 約 **{mt_update['combined_sigma_sr']:.3f}**，對應 luck-threshold 約 **{mt_update['updated_luck_threshold_combined_sigma']:.2f}**。",
            "- 換句話說：本輪若只有小幅優於 baseline、但 Sharpe 仍遠低於上述 luck-threshold，不能把它當成穩健 alpha，頂多視為下一輪樣本外驗證候選。",
            "",
            "## 結論",
            "",
            "- pullback baseline 仍明顯優於 breakout baseline 的核心特徵，是**交易更少、勝率更高**；但樣本只有 5~6 筆，統計信心仍然很弱。",
            "- 本輪重點不是『硬把 pullback 優化到顯著穩健』，而是檢查：改停損寬度、改 ATR 自適應停損、或關閉 immediate_reverse，是否能在這份短樣本上**清楚打敗 baseline pullback**。",
            "- **樣本內**確實有多個變體優於 baseline：緊停損明顯最差；放寬停損、ATR 停損與停用 immediate_reverse 都比 baseline 好，且 `Disable immediate_reverse` 在 MTX/TX 都同步給出最高 Total Return。",
            "- 但若把『本專案其實已經累積測過 19 個非 baseline 變體』算進去，沒有任何一個 pullback 變體能在 **兩個商品都穩定且明顯** 超越更新後的 luck-threshold，因此**目前不能宣稱已找到穩健優化版**；最合理的結論仍是：`Disable immediate_reverse` 與較寬/ATR 停損值得拿去做更長期樣本外驗證。",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_outputs(results: dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_MD.write_text(_build_markdown(results), encoding="utf-8")
    OUTPUT_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    results = _run_all_variants()
    _write_outputs(results)

    for symbol in SYMBOLS:
        best, worst = _best_variants(results["results_by_symbol"][symbol])
        print(
            f"{symbol}: best={best['variant_label']} total_return={best['total_return_pct']:.2f}% "
            f"sharpe={best['sharpe_ratio'] if best['sharpe_ratio'] is not None else 'N/A'}; "
            f"worst={worst['variant_label']} total_return={worst['total_return_pct']:.2f}%"
        )
    print(f"Markdown written: {OUTPUT_MD}")
    print(f"JSON written: {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
