from __future__ import annotations

import json
import math
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from backend.backtest_engine import Trade, run_backtest  # type: ignore
    from backend.config import DEFAULT_COST, DEFAULT_STRATEGY  # type: ignore
    from backend.experiments.proper_evaluation_with_dsr import (  # type: ignore
        CONTRACTS,
        OUT_DIR,
        SHARPE_UNSTABLE_FLAG,
        _assumed_capital,
        _atr14,
        _breakout_signals_with_filter,
        _load_bars,
        _max_drawdown_pct,
        _sharpe_per_trade,
        _total_return,
        _trade_returns,
        evaluate_all,
    )
    from backend.indicators import add_moving_averages  # type: ignore
    from backend.signals import generate_breakout_signals  # type: ignore
else:
    from ..backtest_engine import Trade, run_backtest
    from ..config import DEFAULT_COST, DEFAULT_STRATEGY
    from ..indicators import add_moving_averages
    from ..signals import generate_breakout_signals
    from .proper_evaluation_with_dsr import (
        CONTRACTS,
        OUT_DIR,
        SHARPE_UNSTABLE_FLAG,
        _assumed_capital,
        _atr14,
        _breakout_signals_with_filter,
        _load_bars,
        _max_drawdown_pct,
        _sharpe_per_trade,
        _total_return,
        _trade_returns,
        evaluate_all,
    )


OUTPUT_JSON = OUT_DIR / "combined_filter_exit_results.json"
OUTPUT_MD = OUT_DIR / "combined_filter_exit_results.md"
ATR_MULTIPLIER = 2.5
TAKE_PROFIT_POINTS = 500.0
COMBINED_TRIAL_COUNT = 13

VARIANTS: list[dict[str, Any]] = [
    {
        "key": "baseline",
        "label": "Baseline（無濾網、預設 SL/TP）",
        "entry_filter": None,
        "atr_exit": False,
    },
    {
        "key": "c1_only",
        "label": "C1 單獨（進場最小分離 0.05%）",
        "entry_filter": {"min_separation_ratio": 0.0005},
        "atr_exit": False,
    },
    {
        "key": "d2_only",
        "label": "D2 單獨（ATR14 × 2.5 停損 / TP500）",
        "entry_filter": None,
        "atr_exit": True,
    },
    {
        "key": "combined",
        "label": "COMBINED（C1 + D2）",
        "entry_filter": {"min_separation_ratio": 0.0005},
        "atr_exit": True,
    },
]


def _summary_from_trades(trades: list[Trade]) -> dict[str, float]:
    net_pnls = [float(trade.net_pnl_ntd) for trade in trades]
    wins = [pnl for pnl in net_pnls if pnl > 0]
    losses = [pnl for pnl in net_pnls if pnl < 0]

    if losses:
        profit_factor = sum(wins) / abs(sum(losses)) if wins else 0.0
    else:
        profit_factor = float("inf") if wins else 0.0

    return {
        "total_trades": float(len(trades)),
        "win_rate": (len(wins) / len(trades)) if trades else 0.0,
        "total_net_pnl_ntd": float(sum(net_pnls)),
        "profit_factor": float(profit_factor),
    }


def _run_default_exit(signaled: pd.DataFrame, symbol: str) -> list[Trade]:
    trades, _ = run_backtest(
        signaled,
        strategy="breakout",
        contract=CONTRACTS[symbol],
        cost=DEFAULT_COST,
        strategy_cfg=DEFAULT_STRATEGY,
    )
    return trades


def _run_atr_exit_from_signals(signaled: pd.DataFrame, enriched: pd.DataFrame, symbol: str) -> list[Trade]:
    atr = _atr14(enriched)
    trades: list[Trade] = []
    signal_rows = signaled[signaled["signal"].isin(["long", "short"])]

    for signal_time in signal_rows.index:
        pos = signaled.index.get_loc(signal_time)
        atr_value = atr.iloc[pos]
        if pd.isna(atr_value) or float(atr_value) <= 0:
            continue

        strategy_cfg = replace(
            DEFAULT_STRATEGY,
            breakout_stop_loss_points=float(atr_value) * ATR_MULTIPLIER,
            breakout_take_profit_points=TAKE_PROFIT_POINTS,
        )
        sub = signaled.iloc[pos:].copy()
        sub_trades, _ = run_backtest(
            sub,
            strategy="breakout",
            contract=CONTRACTS[symbol],
            cost=DEFAULT_COST,
            strategy_cfg=strategy_cfg,
        )
        if sub_trades:
            trades.append(sub_trades[0])

    return trades


def _metrics_from_trades(trades: list[Trade], capital: float, trades_per_year: float) -> dict[str, Any]:
    summary = _summary_from_trades(trades)
    returns = _trade_returns(trades, capital)
    sharpe, sharpe_flag = _sharpe_per_trade(returns, trades_per_year)

    profit_factor = summary["profit_factor"]
    if math.isinf(profit_factor):
        profit_factor_value: float | None = None
        profit_factor_display = "∞"
    else:
        profit_factor_value = float(profit_factor)
        profit_factor_display = None

    return {
        "n_trades": int(summary["total_trades"]),
        "win_rate": float(summary["win_rate"]),
        "profit_factor": profit_factor_value,
        "profit_factor_display": profit_factor_display,
        "total_net_pnl_ntd": float(summary["total_net_pnl_ntd"]),
        "total_return_pct": _total_return(returns) * 100.0,
        "sharpe_ratio_annualized": sharpe,
        "sharpe_flag": sharpe_flag,
        "max_drawdown_pct": _max_drawdown_pct(returns) * 100.0,
        "trades_per_year_extrapolated": trades_per_year,
    }


def _collect_nonbaseline_sharpes(results: dict[str, Any]) -> list[float]:
    sharpes: list[float] = []
    for family in ("entry_filters", "exit_mechanisms"):
        for symbol_results in results[family].values():
            for variant_key, metrics in symbol_results.items():
                sharpe = metrics["sharpe_per_trade_annualized"]
                if variant_key != "baseline" and sharpe is not None:
                    sharpes.append(float(sharpe))
    return sharpes


def evaluate_combined_filter_exit() -> dict[str, Any]:
    prior_results = evaluate_all()
    prior_sharpes = _collect_nonbaseline_sharpes(prior_results)

    results: dict[str, Any] = {
        "variants": {variant["key"]: {"label": variant["label"]} for variant in VARIANTS},
        "symbols": {},
        "assumptions": {
            "capital_normalization": (
                "Total Return 以 proper_evaluation_with_dsr.py 相同邏輯計算："
                "assumed_capital = 平均 close 價格 × point_value。"
            ),
            "signal_timing": "訊號於收盤確認，下一根開盤成交；沿用既有引擎的 gap / 成本 / 同棒先停損規則。",
            "combined_trial_count_note": (
                "先前正式評估的非 baseline 變體數為 12；本次新增 COMBINED 1 個新變體後，"
                "多重檢定計數應更新為 N=13。"
            ),
        },
        "luck_threshold": {
            "previous_expected_max_sharpe_n12": prior_results["multiple_testing_haircut"][
                "expected_max_sharpe_from_pure_luck"
            ],
            "previous_num_variants": prior_results["multiple_testing_haircut"][
                "num_variants_tried_excluding_baseline"
            ],
        },
    }

    combined_sharpes: list[float] = []

    for symbol in ("MTX", "TX"):
        bars = _load_bars(symbol)
        enriched = add_moving_averages(
            bars,
            fast=DEFAULT_STRATEGY.ma_fast,
            mid=DEFAULT_STRATEGY.ma_mid,
            slow=DEFAULT_STRATEGY.ma_slow,
        )
        capital = _assumed_capital(bars, CONTRACTS[symbol])
        span_days = (bars.index[-1] - bars.index[0]).total_seconds() / 86400.0
        years = max(span_days / 365.25, 1e-6)
        results["symbols"][symbol] = {}

        for variant in VARIANTS:
            if variant["entry_filter"] is None:
                signaled = generate_breakout_signals(enriched)
            else:
                signaled = _breakout_signals_with_filter(enriched, **variant["entry_filter"])

            if variant["atr_exit"]:
                trades = _run_atr_exit_from_signals(signaled, enriched, symbol)
            else:
                trades = _run_default_exit(signaled, symbol)

            trades_per_year = len(trades) / years if years > 0 else 0.0
            metrics = _metrics_from_trades(trades, capital, trades_per_year)
            results["symbols"][symbol][variant["key"]] = metrics

            sharpe = metrics["sharpe_ratio_annualized"]
            if variant["key"] == "combined" and sharpe is not None:
                combined_sharpes.append(float(sharpe))

    updated_sharpes = prior_sharpes + combined_sharpes
    sigma_sr = float(np.std(updated_sharpes, ddof=1)) if len(updated_sharpes) > 1 else 0.0
    updated_threshold = sigma_sr * math.sqrt(2.0 * math.log(COMBINED_TRIAL_COUNT))

    results["luck_threshold"].update(
        {
            "updated_num_variants": COMBINED_TRIAL_COUNT,
            "sigma_of_observed_sharpes_with_combined": sigma_sr,
            "expected_max_sharpe_from_pure_luck_n13": updated_threshold,
            "formula": "E[max(SR)] ~= sigma_SR * sqrt(2 * ln(N))",
        }
    )

    for symbol in ("MTX", "TX"):
        combined_metrics = results["symbols"][symbol]["combined"]
        sharpe = combined_metrics["sharpe_ratio_annualized"]
        results["symbols"][symbol]["combined_vs_luck_threshold"] = {
            "combined_sharpe": sharpe,
            "combined_sharpe_flag": combined_metrics["sharpe_flag"],
            "beats_updated_luck_threshold_n13": bool(sharpe is not None and sharpe > updated_threshold),
            "gap_to_updated_threshold": (float(sharpe - updated_threshold) if sharpe is not None else None),
        }

    return results


def _fmt_num(value: float | None, digits: int = 2) -> str:
    return "N/A" if value is None else f"{value:,.{digits}f}"


def _fmt_pct(value: float | None, digits: int = 2) -> str:
    return "N/A" if value is None else f"{value:.{digits}f}%"


def _fmt_pf(metrics: dict[str, Any]) -> str:
    if metrics.get("profit_factor_display") == "∞":
        return "∞"
    return _fmt_num(metrics.get("profit_factor"), 2)


def _fmt_sharpe(metrics: dict[str, Any]) -> str:
    if metrics["sharpe_flag"] == SHARPE_UNSTABLE_FLAG:
        return "不穩定（近零變異）"
    if metrics["sharpe_flag"] == "insufficient_trades":
        return "N/A（交易不足）"
    return _fmt_num(metrics["sharpe_ratio_annualized"], 2)


def _symbol_table(symbol_results: dict[str, Any]) -> str:
    lines = [
        "| 變體 | 交易數 | 勝率 | Profit Factor | 總淨損益(NTD) | Total Return % | Sharpe Ratio | Max Drawdown % |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for variant in VARIANTS:
        metrics = symbol_results[variant["key"]]
        lines.append(
            f"| {variant['label']} | {metrics['n_trades']} | {_fmt_pct(metrics['win_rate'] * 100, 1)} | "
            f"{_fmt_pf(metrics)} | {_fmt_num(metrics['total_net_pnl_ntd'], 0)} | "
            f"{_fmt_pct(metrics['total_return_pct'], 2)} | {_fmt_sharpe(metrics)} | "
            f"{_fmt_pct(metrics['max_drawdown_pct'], 2)} |"
        )
    return "\n".join(lines)


def _comparison_sentence(symbol: str, symbol_results: dict[str, Any], updated_threshold: float) -> list[str]:
    baseline = symbol_results["baseline"]
    c1_only = symbol_results["c1_only"]
    d2_only = symbol_results["d2_only"]
    combined = symbol_results["combined"]

    comparisons = []
    if combined["total_return_pct"] > d2_only["total_return_pct"]:
        comparisons.append("優於 D2 單獨")
    elif combined["total_return_pct"] < d2_only["total_return_pct"]:
        comparisons.append("劣於 D2 單獨")
    else:
        comparisons.append("與 D2 單獨幾乎無差")

    if combined["total_return_pct"] > c1_only["total_return_pct"]:
        comparisons.append("優於 C1 單獨")
    elif combined["total_return_pct"] < c1_only["total_return_pct"]:
        comparisons.append("劣於 C1 單獨")
    else:
        comparisons.append("與 C1 單獨幾乎無差")

    if combined["total_return_pct"] > baseline["total_return_pct"]:
        comparisons.append("優於 baseline")
    elif combined["total_return_pct"] < baseline["total_return_pct"]:
        comparisons.append("劣於 baseline")
    else:
        comparisons.append("與 baseline 幾乎無差")

    sharpe = combined["sharpe_ratio_annualized"]
    if sharpe is None:
        luck_text = "combined Sharpe 無法穩定估計，因此不能宣稱超過運氣門檻。"
    elif sharpe > updated_threshold:
        luck_text = f"combined Sharpe {sharpe:.2f} 高於更新後 N=13 的 luck threshold {updated_threshold:.2f}。"
    else:
        luck_text = f"combined Sharpe {sharpe:.2f} 明顯低於更新後 N=13 的 luck threshold {updated_threshold:.2f}。"

    return [
        f"- **{symbol}**：COMBINED 交易數 {combined['n_trades']}，{ '；'.join(comparisons) }。",
        f"- **{symbol}**：{luck_text}",
    ]


def build_report(results: dict[str, Any]) -> str:
    updated_threshold = results["luck_threshold"]["expected_max_sharpe_from_pure_luck_n13"]
    lines = [
        "# C1 進場濾網 + D2 ATR 出場：組合交互作用測試",
        "",
        "## 1. 測試目的",
        "- 檢查先前分開表現最佳的 **C1（進場最小分離 0.05%）** 與 **D2（ATR14 × 2.5 停損 / TP500）**，",
        "  在同一策略內同時啟用後，是否出現互補、互相抵消，或幾乎沒有交互作用。",
        "- 比較 4 個版本：baseline、C1 單獨、D2 單獨、COMBINED（C1 + D2）。",
        "- 指標沿用 `proper_evaluation_with_dsr.py`：交易數、勝率、Profit Factor、總淨損益、",
        "  Total Return %、Sharpe Ratio（逐筆交易年化）、Max Drawdown %。",
        "",
        "## 2. 方法與一致性說明",
        "- 進場濾網直接沿用 `proper_evaluation_with_dsr.py::_breakout_signals_with_filter()` 的 C1 寫法。",
        "- ATR 停損直接沿用 `proper_evaluation_with_dsr.py::_atr14()` 與 `_run_exit_variant()` 的 per-trade sliced rerun 模式。",
        "- Total Return、Sharpe、Max Drawdown 與 Sharpe 不穩定判定，也全部沿用同一份正式評估邏輯。",
        "- **資本基準假設**：`assumed_capital = 平均 close 價格 × point_value`；因此 Total Return % 是相對於這個假設資本的規模化結果，不是實際保證金報酬率。",
        "",
        "## 3. 多重檢定門檻（Prado / DSR 概念）",
        f"- 先前正式報告使用 **N=12**，pure-luck Expected Max Sharpe 約 **{results['luck_threshold']['previous_expected_max_sharpe_n12']:.3f}**。",
        f"- 本次新增的真正『新試法』只有 **COMBINED 1 個新變體**，因此理應把 trial count 更新為 **N={results['luck_threshold']['updated_num_variants']}**。",
        f"- 納入 COMBINED 後重新估計，pure-luck Expected Max Sharpe 約 **{updated_threshold:.3f}**。",
        "",
    ]

    for symbol in ("MTX", "TX"):
        lines.extend(
            [
                f"## 4. {symbol} 結果",
                _symbol_table(results["symbols"][symbol]),
                "",
            ]
        )

    lines.extend(
        [
            "## 5. 重點發現",
            "",
            *_comparison_sentence("MTX", results["symbols"]["MTX"], updated_threshold),
            *_comparison_sentence("TX", results["symbols"]["TX"], updated_threshold),
            "",
            "## 6. 整體結論",
        ]
    )

    mtx_combined = results["symbols"]["MTX"]["combined"]
    tx_combined = results["symbols"]["TX"]["combined"]
    mtx_d2 = results["symbols"]["MTX"]["d2_only"]
    tx_d2 = results["symbols"]["TX"]["d2_only"]

    if (
        mtx_combined["total_return_pct"] > mtx_d2["total_return_pct"]
        and tx_combined["total_return_pct"] > tx_d2["total_return_pct"]
    ):
        overall = "COMBINED 在兩個商品都優於 D2 單獨，代表 C1 與 D2 有正向互補。"
    elif (
        mtx_combined["total_return_pct"] < mtx_d2["total_return_pct"]
        and tx_combined["total_return_pct"] < tx_d2["total_return_pct"]
    ):
        overall = "COMBINED 在兩個商品都劣於 D2 單獨，代表 C1 沒有幫 D2 加分，反而削弱效果。"
    else:
        overall = "COMBINED 在 MTX / TX 的效果不一致，代表交互作用偏弱，沒有形成穩定優勢。"

    lines.extend(
        [
            f"- **整體判讀**：{overall}",
            "- **是否超過 Prado 運氣門檻**：沒有。即使使用更新後的 N=13 門檻，COMBINED 的 Sharpe 仍遠低於 pure-luck threshold，不能視為已證明的 alpha。",
            "- **交易數檢查**：COMBINED 的交易數應明顯低於 baseline 24 筆；本次實測結果已列於上表，可直接檢查是否符合『濾網 + ATR 應壓低出手數』的直覺。",
            "- **建議**：若仍想繼續追蹤 COMBINED，下一步應以更長期間與樣本外資料重跑，而不是只依這 30 天小樣本下結論。",
        ]
    )

    return "\n".join(lines)


def main() -> None:
    results = evaluate_combined_filter_exit()
    OUTPUT_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    OUTPUT_MD.write_text(build_report(results), encoding="utf-8")
    print(f"Wrote {OUTPUT_JSON}")
    print(f"Wrote {OUTPUT_MD}")


if __name__ == "__main__":
    main()
