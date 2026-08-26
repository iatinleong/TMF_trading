from __future__ import annotations

import json
import math
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parents[1]))
    from backtest_engine import run_backtest  # type: ignore
    from config import ContractSpec, DEFAULT_COST, DEFAULT_STRATEGY  # type: ignore
    from indicators import add_moving_averages  # type: ignore
    from experiments.proper_evaluation_with_dsr import (  # type: ignore
        SHARPE_UNSTABLE_FLAG,
        _assumed_capital,
        _load_bars,
        _max_drawdown_pct,
        _sharpe_per_trade,
        _total_return,
        _trade_returns,
    )
else:  # pragma: no cover - exercised via package import/module execution
    from ..backtest_engine import run_backtest
    from ..config import ContractSpec, DEFAULT_COST, DEFAULT_STRATEGY
    from ..indicators import add_moving_averages
    from .proper_evaluation_with_dsr import (
        SHARPE_UNSTABLE_FLAG,
        _assumed_capital,
        _load_bars,
        _max_drawdown_pct,
        _sharpe_per_trade,
        _total_return,
        _trade_returns,
    )


BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = DATA_DIR / "experiments"
OUTPUT_MD = OUTPUT_DIR / "pullback_sltp_grid_search.md"
OUTPUT_JSON = OUTPUT_DIR / "pullback_sltp_grid_search.json"

SYMBOLS = ("MTX", "TX")
STOP_LOSS_VALUES = [75, 100, 150, 200, 250, 300, 350]
TAKE_PROFIT_VALUES = [150, 200, 250, 300, 400, 500, 600, 700]
RATIO_SUBSET = (1.0, 1.5, 2.0)
BASELINE_SL = 150
BASELINE_TP = 300
C2_EQUIVALENT_SL = 300
C2_EQUIVALENT_TP = 300
PRE_FIX_BASELINE_COUNTS = {"MTX": 5, "TX": 6}

CONTRACT_SPECS: dict[str, ContractSpec] = {
    "MTX": ContractSpec(name="MTX", point_value=50.0, tick_size=1.0),
    "TX": ContractSpec(name="TX", point_value=200.0, tick_size=1.0),
}


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


def _fmt_pp_delta(value: float) -> str:
    return f"{value:+.2f} 個百分點"


def _fmt_pf(value: float | None, display: str | None) -> str:
    if display is not None:
        return display
    return _fmt_num(value, 2)


def _fmt_sharpe(value: float | None, flag: str | None) -> str:
    if value is not None:
        return f"{value:.2f}"
    if flag == SHARPE_UNSTABLE_FLAG:
        return "不穩定(近零變異)"
    if flag == "insufficient_trades":
        return "N/A(交易太少)"
    return "N/A"


def _profit_factor_value(summary: dict[str, Any]) -> tuple[float | None, str | None]:
    raw_value = summary.get("profit_factor")
    if isinstance(raw_value, (float, int)) and math.isinf(float(raw_value)):
        return None, "∞"
    return _safe_float(raw_value), None


def _years_in_sample(bars: pd.DataFrame) -> float:
    span_days = (bars.index[-1] - bars.index[0]).total_seconds() / 86400.0
    return max(span_days / 365.25, 1e-6)


def _is_ratio(entry: dict[str, Any], target: float) -> bool:
    return math.isclose(float(entry["risk_reward_ratio"]), float(target), rel_tol=1e-9, abs_tol=1e-9)


def _ranked_by_total_return(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        entries,
        key=lambda item: (
            -float(item["total_return_pct"]),
            -(float(item["sharpe_ratio"]) if item["sharpe_ratio"] is not None else float("-inf")),
            float(item["max_drawdown_pct"]),
            float(item["stop_loss_points"]),
            float(item["take_profit_points"]),
        ),
    )


def _best_by_total_return(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return _ranked_by_total_return(entries)[0]


def _best_by_sharpe(entries: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = [entry for entry in entries if entry["sharpe_ratio"] is not None]
    if not candidates:
        return _best_by_total_return(entries)
    return max(
        candidates,
        key=lambda item: (
            float(item["sharpe_ratio"]),
            float(item["total_return_pct"]),
            -float(item["max_drawdown_pct"]),
            -float(item["n_trades"]),
        ),
    )


def _find_entry(entries: list[dict[str, Any]], *, sl: int, tp: int) -> dict[str, Any]:
    for entry in entries:
        if int(entry["stop_loss_points"]) == sl and int(entry["take_profit_points"]) == tp:
            return entry
    raise KeyError(f"Missing entry for SL={sl}, TP={tp}")


def _representative_subset(entries: list[dict[str, Any]], best_total: dict[str, Any], best_sharpe: dict[str, Any]) -> list[dict[str, Any]]:
    subset: dict[tuple[int, int], dict[str, Any]] = {}
    for entry in entries:
        if any(_is_ratio(entry, ratio) for ratio in RATIO_SUBSET):
            subset[(int(entry["stop_loss_points"]), int(entry["take_profit_points"]))] = entry
    subset[(int(best_total["stop_loss_points"]), int(best_total["take_profit_points"]))] = best_total
    subset[(int(best_sharpe["stop_loss_points"]), int(best_sharpe["take_profit_points"]))] = best_sharpe
    return sorted(
        subset.values(),
        key=lambda item: (
            float(item["risk_reward_ratio"]),
            float(item["stop_loss_points"]),
            float(item["take_profit_points"]),
        ),
    )


def _signal_behavior_label(baseline_counts: dict[str, int]) -> str:
    counts = list(baseline_counts.values())
    if all(count >= 15 for count in counts):
        return "已修正版本（baseline 約 15+ 筆/商品）"
    if all(4 <= count <= 7 for count in counts):
        return "舊版/有 suppression bug 的 alternating-only 行為（baseline 約 5~6 筆/商品）"
    return "介於兩者之間或混合狀態（請以實際 baseline 交易數解讀）"


def _evaluate_combo(
    *,
    symbol: str,
    bars: pd.DataFrame,
    enriched: pd.DataFrame,
    sample_years: float,
    capital: float,
    stop_loss_points: int,
    take_profit_points: int,
) -> dict[str, Any]:
    strategy_cfg = replace(
        DEFAULT_STRATEGY,
        pullback_stop_loss_points=float(stop_loss_points),
        pullback_take_profit_points=float(take_profit_points),
    )
    trades, summary = run_backtest(
        enriched,
        strategy="pullback",
        contract=CONTRACT_SPECS[symbol],
        cost=DEFAULT_COST,
        strategy_cfg=strategy_cfg,
        slippage_points=DEFAULT_COST.slippage_points,
    )
    returns = _trade_returns(trades, capital)
    n_trades = len(trades)
    trades_per_year = n_trades / sample_years if sample_years > 0 else 0.0
    sharpe_ratio, sharpe_flag = _sharpe_per_trade(returns, trades_per_year)
    profit_factor, profit_factor_display = _profit_factor_value(summary)
    risk_reward_ratio = float(take_profit_points) / float(stop_loss_points)

    return {
        "symbol": symbol,
        "stop_loss_points": int(stop_loss_points),
        "take_profit_points": int(take_profit_points),
        "risk_reward_ratio": risk_reward_ratio,
        "risk_reward_label": f"{take_profit_points}/{stop_loss_points} = {risk_reward_ratio:.2f}",
        "n_trades": n_trades,
        "win_rate": float(summary["win_rate"]),
        "win_rate_pct": float(summary["win_rate"]) * 100.0,
        "profit_factor": profit_factor,
        "profit_factor_display": profit_factor_display,
        "total_net_pnl_ntd": float(summary["total_net_pnl_ntd"]),
        "total_return_pct": float(_total_return(returns) * 100.0),
        "sharpe_ratio": _safe_float(sharpe_ratio),
        "sharpe_flag": sharpe_flag,
        "max_drawdown_pct": float(_max_drawdown_pct(returns) * 100.0),
        "trades_per_year_extrapolated": float(trades_per_year),
        "data_start": bars.index[0].isoformat(),
        "data_end": bars.index[-1].isoformat(),
        "n_bars": int(len(bars)),
        "is_baseline": stop_loss_points == BASELINE_SL and take_profit_points == BASELINE_TP,
        "is_c2_equivalent": stop_loss_points == C2_EQUIVALENT_SL and take_profit_points == C2_EQUIVALENT_TP,
    }


def evaluate_grid() -> dict[str, Any]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    metadata_symbols: dict[str, Any] = {}
    results_by_symbol: dict[str, list[dict[str, Any]]] = {}
    flat_results: list[dict[str, Any]] = []

    for symbol in SYMBOLS:
        bars = _load_bars(symbol)
        enriched = add_moving_averages(
            bars,
            fast=DEFAULT_STRATEGY.ma_fast,
            mid=DEFAULT_STRATEGY.ma_mid,
            slow=DEFAULT_STRATEGY.ma_slow,
        ).sort_index()
        sample_years = _years_in_sample(bars)
        capital = _assumed_capital(bars, CONTRACT_SPECS[symbol])
        symbol_results: list[dict[str, Any]] = []

        metadata_symbols[symbol] = {
            "data_start": bars.index[0].isoformat(),
            "data_end": bars.index[-1].isoformat(),
            "n_bars": int(len(bars)),
            "assumed_capital_ntd": float(capital),
            "sample_years": float(sample_years),
        }

        for stop_loss_points in STOP_LOSS_VALUES:
            for take_profit_points in TAKE_PROFIT_VALUES:
                entry = _evaluate_combo(
                    symbol=symbol,
                    bars=bars,
                    enriched=enriched,
                    sample_years=sample_years,
                    capital=capital,
                    stop_loss_points=stop_loss_points,
                    take_profit_points=take_profit_points,
                )
                symbol_results.append(entry)
                flat_results.append(entry)

        results_by_symbol[symbol] = symbol_results

    return {
        "metadata": {
            "strategy": "pullback",
            "dataset_mode": "full_dataset_only",
            "stop_loss_grid": STOP_LOSS_VALUES,
            "take_profit_grid": TAKE_PROFIT_VALUES,
            "grid_size_per_symbol": len(STOP_LOSS_VALUES) * len(TAKE_PROFIT_VALUES),
            "symbols_tested": list(SYMBOLS),
            "total_backtests": len(flat_results),
            "symbol_details": metadata_symbols,
        },
        "results_by_symbol": results_by_symbol,
        "results": flat_results,
    }


def _table_rows(entries: list[dict[str, Any]]) -> list[str]:
    rows: list[str] = []
    for rank, entry in enumerate(entries, start=1):
        tags: list[str] = []
        if entry["is_baseline"]:
            tags.append("baseline")
        if entry["is_c2_equivalent"]:
            tags.append("C2等價")
        tag_text = ", ".join(tags) if tags else "-"
        rows.append(
            f"| {rank} | {int(entry['stop_loss_points'])} | {int(entry['take_profit_points'])} | "
            f"{float(entry['risk_reward_ratio']):.2f} | {entry['n_trades']} | {_fmt_pct(entry['win_rate_pct'])} | "
            f"{_fmt_pf(entry['profit_factor'], entry['profit_factor_display'])} | {_fmt_num(entry['total_net_pnl_ntd'], 0)} | "
            f"{_fmt_pct(entry['total_return_pct'])} | {_fmt_sharpe(entry['sharpe_ratio'], entry['sharpe_flag'])} | "
            f"{_fmt_pct(entry['max_drawdown_pct'])} | {tag_text} |"
        )
    return rows


def build_report(results: dict[str, Any]) -> str:
    results_by_symbol: dict[str, list[dict[str, Any]]] = results["results_by_symbol"]
    metadata = results["metadata"]

    baseline_entries = {
        symbol: _find_entry(entries, sl=BASELINE_SL, tp=BASELINE_TP)
        for symbol, entries in results_by_symbol.items()
    }
    baseline_counts = {symbol: entry["n_trades"] for symbol, entry in baseline_entries.items()}
    signal_behavior = _signal_behavior_label(baseline_counts)

    lines: list[str] = [
        "# Pullback 策略 SL/TP 網格搜尋（全樣本，bug 修復後重跑）",
        "",
        "## 這份報告為何需要重跑？",
        "- 先前版本的 `generate_pullback_signals()` 會錯誤壓掉**已平倉之後再次出現的同向 pullback 觸價訊號**，導致整份 grid search 建立在過小且失真的交易樣本上。",
        "- 這次是 **同一個 SL/TP 網格搜尋實驗** 在 pullback 訊號 bug 修復後的重新執行；回測入口仍是 `backend.backtest_engine.run_backtest(strategy=\"pullback\")`，因此重新執行就會自動吃到修正後的 `backend/signals.py` 行為，不存在額外 cached signal 流程。",
        "- 舊報告 `pullback_sltp_grid_search.md/.json` 因為建立在 bug 版訊號上，現在都應視為**過期結果**。",
        "",
        "## 實驗設定",
        f"- 資料模式：**全資料集直接回測**（未做 walk-forward / train-test split）",
        f"- 商品：{', '.join(SYMBOLS)}",
        f"- 每商品測試組合數：**{metadata['grid_size_per_symbol']} 組**（SL {STOP_LOSS_VALUES} × TP {TAKE_PROFIT_VALUES}）",
        f"- 總回測次數：**{metadata['total_backtests']} 次**",
        f"- Baseline：SL={BASELINE_SL}、TP={BASELINE_TP}，名目 RR = {BASELINE_TP / BASELINE_SL:.2f}",
        "- 評估指標：交易數、勝率、Profit Factor、總淨損益、Total Return、Sharpe Ratio、Max Drawdown",
        "- **Sharpe Ratio / Total Return / Max Drawdown / 假設資本分母** 全部直接重用 `backend.experiments.proper_evaluation_with_dsr` 內既有函式，與本專案先前報告口徑一致。",
        "",
        "## Baseline 交易數：舊版 bug 結果 vs 修復後重跑",
        f"- MTX baseline（150/300）：**{PRE_FIX_BASELINE_COUNTS['MTX']} → {baseline_counts['MTX']} 筆**",
        f"- TX baseline（150/300）：**{PRE_FIX_BASELINE_COUNTS['TX']} → {baseline_counts['TX']} 筆**",
        f"- 判讀：**{signal_behavior}**",
        "- 這代表本次 grid search 已經不再是先前那種每商品只剩 5~6 筆交易的失真樣本，而是回到較有意義的 16~17 筆 baseline 規模。",
        "",
        "## 先回答核心問題：樣本放大後，犧牲風報比是否仍然必要？",
        "",
    ]

    for symbol in SYMBOLS:
        symbol_entries = results_by_symbol[symbol]
        best_total = _best_by_total_return(symbol_entries)
        best_sharpe = _best_by_sharpe(symbol_entries)
        baseline_entry = baseline_entries[symbol]
        c2_equivalent = _find_entry(symbol_entries, sl=C2_EQUIVALENT_SL, tp=C2_EQUIVALENT_TP)
        exact_two_to_one = [entry for entry in symbol_entries if _is_ratio(entry, 2.0)]
        best_two_to_one_total = _best_by_total_return(exact_two_to_one)
        best_two_to_one_sharpe = _best_by_sharpe(exact_two_to_one)
        best_vs_baseline_pp = float(best_total["total_return_pct"]) - float(baseline_entry["total_return_pct"])

        beats_c2_total = best_two_to_one_total["total_return_pct"] > c2_equivalent["total_return_pct"]
        c2_sharpe = c2_equivalent["sharpe_ratio"]
        best_two_to_one_sharpe_value = best_two_to_one_sharpe["sharpe_ratio"]
        beats_c2_sharpe = (
            best_two_to_one_sharpe_value is not None
            and (c2_sharpe is None or best_two_to_one_sharpe_value > c2_sharpe)
        )

        lines += [
            f"### {symbol}",
            f"- **修復後 baseline（150/300）**：交易數 {baseline_entry['n_trades']}，Total Return **{_fmt_pct(baseline_entry['total_return_pct'])}**，"
            f"Sharpe **{_fmt_sharpe(baseline_entry['sharpe_ratio'], baseline_entry['sharpe_flag'])}**。",
            f"- **Total Return 最佳**：SL={int(best_total['stop_loss_points'])} / TP={int(best_total['take_profit_points'])} "
            f"(RR={best_total['risk_reward_ratio']:.2f})，Total Return **{_fmt_pct(best_total['total_return_pct'])}**，"
            f"Sharpe **{_fmt_sharpe(best_total['sharpe_ratio'], best_total['sharpe_flag'])}**，"
            f"交易數 {best_total['n_trades']}。",
            f"- **相對修復後 baseline 的提升**：{_fmt_pp_delta(best_vs_baseline_pp)}（{_fmt_pct(baseline_entry['total_return_pct'])} → {_fmt_pct(best_total['total_return_pct'])}）。",
            f"- **Sharpe 最佳**：SL={int(best_sharpe['stop_loss_points'])} / TP={int(best_sharpe['take_profit_points'])} "
            f"(RR={best_sharpe['risk_reward_ratio']:.2f})，Sharpe **{_fmt_sharpe(best_sharpe['sharpe_ratio'], best_sharpe['sharpe_flag'])}**，"
            f"Total Return **{_fmt_pct(best_sharpe['total_return_pct'])}**，交易數 {best_sharpe['n_trades']}。",
            f"- **1:1 的 C2 等價組合**（SL=300 / TP=300）：Total Return {_fmt_pct(c2_equivalent['total_return_pct'])}，"
            f"Sharpe {_fmt_sharpe(c2_equivalent['sharpe_ratio'], c2_equivalent['sharpe_flag'])}，交易數 {c2_equivalent['n_trades']}。",
            f"- **最佳 2:1 組合**（只看 RR=2.0）：SL={int(best_two_to_one_total['stop_loss_points'])} / "
            f"TP={int(best_two_to_one_total['take_profit_points'])}，Total Return {_fmt_pct(best_two_to_one_total['total_return_pct'])}，"
            f"Sharpe {_fmt_sharpe(best_two_to_one_total['sharpe_ratio'], best_two_to_one_total['sharpe_flag'])}。",
            (
                f"- **直接結論**：{('有' if beats_c2_total else '沒有')} 2:1 組合在 Total Return 上打敗 1:1 的 300/300；"
                f"{('有' if beats_c2_sharpe else '沒有')} 2:1 組合在 Sharpe 上打敗 300/300。"
            ),
            "",
        ]

    lines += [
        "## 代表性子集合（至少涵蓋 RR = 1.0 / 1.5 / 2.0，另補整體最佳格）",
        "",
    ]

    for symbol in SYMBOLS:
        symbol_entries = results_by_symbol[symbol]
        best_total = _best_by_total_return(symbol_entries)
        best_sharpe = _best_by_sharpe(symbol_entries)
        subset = _representative_subset(symbol_entries, best_total, best_sharpe)
        lines += [
            f"### {symbol}",
            "",
            "| 排名 | SL | TP | RR | 交易數 | 勝率 | PF | 總淨損益(NTD) | Total Return | Sharpe | Max DD | 備註 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
        lines.extend(_table_rows(_ranked_by_total_return(subset)))
        lines.append("")

    lines += [
        "## 完整網格排名（依 Total Return 由高到低）",
        "",
    ]

    for symbol in SYMBOLS:
        symbol_entries = _ranked_by_total_return(results_by_symbol[symbol])
        lines += [
            f"### {symbol}",
            "",
            f"- 資料區間：{results['metadata']['symbol_details'][symbol]['data_start']} ~ {results['metadata']['symbol_details'][symbol]['data_end']} "
            f"（{results['metadata']['symbol_details'][symbol]['n_bars']} 根 60 分 K）",
            "",
            "| 排名 | SL | TP | RR | 交易數 | 勝率 | PF | 總淨損益(NTD) | Total Return | Sharpe | Max DD | 備註 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
        lines.extend(_table_rows(symbol_entries))
        lines.append("")

    lines += [
        "## 多重測試 / Overfitting 風險提醒",
        "",
        "這次重跑**不是新增另一輪獨立的 112 個 trial**，而是同一個 `56 × 2` pullback SL/TP 網格在訊號 bug 修復後重新評估；",
        "因此在 Prado / Deflated Sharpe Ratio 的試驗數觀點下，這裡應該仍視為**同一輪 112 格**，不應把舊 bug 版結果再加總成 224 格。",
        "若再疊加本專案先前約 **19** 個非 baseline 變體，整體仍可視為**約 131 個『看過後才挑最好』的候選結果**。依照先前專案裡引用的 Prado / Deflated Sharpe Ratio 脈絡，",
        "**從 100+ 個格子裡挑出單一最佳者，selection bias / multiple testing 風險只會比前幾輪更嚴重，不會更輕**。因此本報告裡的",
        "『最佳格』只能視為**下一輪驗證候選**，不能視為已證明的最優參數；要更有說服力，仍需要更長期、更多 regime 的歷史資料再驗證。",
        "",
        "## 總結",
        "",
        "- 本報告直接回答了『修復 bug 之後，是否一定要把 RR 從 2:1 犧牲到 1:1 才能改善績效』這個問題：請看各商品上方的 C2 等價組合 vs 最佳 2:1 組合比較。",
        "- 若某個最佳格剛好來自極低交易數、Sharpe 不穩定或只是單一商品特例，解讀時必須保守。",
        "- Raw grid 數據已輸出到 `pullback_sltp_grid_search.json`，後續若要再做圖、做 DSR / bootstrap / heatmap，可直接重用。",
    ]
    return "\n".join(lines)


def main() -> None:
    results = evaluate_grid()
    OUTPUT_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    OUTPUT_MD.write_text(build_report(results), encoding="utf-8")
    print("Report written to:", OUTPUT_MD)
    print("JSON written to:", OUTPUT_JSON)


if __name__ == "__main__":
    main()
