from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

EXPERIMENTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = EXPERIMENTS_DIR.parent
REPO_DIR = BACKEND_DIR.parent
DATA_DIR = REPO_DIR / "data"
OUTPUT_DIR = DATA_DIR / "experiments"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

try:
    from ..backtest_engine import run_backtest, trades_to_dataframe
    from ..config import ContractSpec, DEFAULT_COST, DEFAULT_STRATEGY
    from ..indicators import add_moving_averages
    from ..signals import _apply_signal_columns
    from .proper_evaluation_with_dsr import (
        SHARPE_UNSTABLE_FLAG,
        _assumed_capital,
        _atr14,
        _max_drawdown_pct,
        _sharpe_per_trade,
        _total_return,
        _trade_returns,
    )
except ImportError:  # pragma: no cover - script execution fallback
    from backtest_engine import run_backtest, trades_to_dataframe  # type: ignore
    from config import ContractSpec, DEFAULT_COST, DEFAULT_STRATEGY  # type: ignore
    from indicators import add_moving_averages  # type: ignore
    from signals import _apply_signal_columns  # type: ignore
    from proper_evaluation_with_dsr import (  # type: ignore
        SHARPE_UNSTABLE_FLAG,
        _assumed_capital,
        _atr14,
        _max_drawdown_pct,
        _sharpe_per_trade,
        _total_return,
        _trade_returns,
    )


CONTRACT_SPECS: dict[str, ContractSpec] = {
    "MTX": ContractSpec(name="MTX", point_value=50.0, tick_size=1.0),
    "TX": ContractSpec(name="TX", point_value=200.0, tick_size=1.0),
}

PURE_LUCK_SHARPE_THRESHOLD = 4.178
PURE_LUCK_SHARPE_THRESHOLD_SOURCE = "data/experiments/proper_evaluation_with_dsr.md"
SAMPLE_SIZE_CAVEAT = (
    "樣本期間僅約 30 個交易日、每個商品 baseline 只有 24 筆交易；"
    "任何濾網造成的勝率、Sharpe 與同棒停損比例改善，都可能受小樣本影響。"
)


@dataclass(frozen=True)
class Variant:
    key: str
    label: str
    description: str
    min_adx: float | None = None
    require_atr_expansion: bool = False


VARIANTS: list[Variant] = [
    Variant(key="baseline", label="A. Baseline", description="原始 breakout 訊號，無 regime filter。"),
    Variant(
        key="adx_gt_20",
        label="B. ADX(14) > 20",
        description="僅在訊號棒 ADX(14) > 20 時保留 MA crossover。",
        min_adx=20.0,
    ),
    Variant(
        key="adx_gt_25",
        label="C. ADX(14) > 25",
        description="僅在訊號棒 ADX(14) > 25 時保留 MA crossover。",
        min_adx=25.0,
    ),
    Variant(
        key="atr_expansion",
        label="D. ATR 擴張",
        description="僅在訊號棒 ATR(14) > ATR(14) 自身 60 根 rolling median 時保留訊號。",
        require_atr_expansion=True,
    ),
    Variant(
        key="adx_gt_20_and_atr_expansion",
        label="E. ADX>20 且 ATR 擴張",
        description="同時要求 ADX(14) > 20 與 ATR(14) 高於自身 60 根 rolling median。",
        min_adx=20.0,
        require_atr_expansion=True,
    ),
]


def _load_bars(symbol: str) -> pd.DataFrame:
    bars = pd.read_csv(DATA_DIR / f"{symbol}_60min_real.csv", parse_dates=["datetime"], index_col="datetime")
    bars.index = pd.to_datetime(bars.index)
    return bars.sort_index()


def _adx(df: pd.DataFrame, window: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    prev_close = close.shift(1)
    true_range = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    alpha = 1.0 / float(window)
    atr_wilder = true_range.ewm(alpha=alpha, adjust=False, min_periods=window).mean()
    plus_dm_smoothed = plus_dm.ewm(alpha=alpha, adjust=False, min_periods=window).mean()
    minus_dm_smoothed = minus_dm.ewm(alpha=alpha, adjust=False, min_periods=window).mean()

    plus_di = 100.0 * plus_dm_smoothed / atr_wilder
    minus_di = 100.0 * minus_dm_smoothed / atr_wilder

    di_sum = plus_di + minus_di
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum.where(di_sum != 0)
    return dx.ewm(alpha=alpha, adjust=False, min_periods=window).mean()


def _prepare_enriched(symbol: str) -> pd.DataFrame:
    bars = _load_bars(symbol)
    enriched = add_moving_averages(
        bars,
        fast=DEFAULT_STRATEGY.ma_fast,
        mid=DEFAULT_STRATEGY.ma_mid,
        slow=DEFAULT_STRATEGY.ma_slow,
    )
    enriched["atr14"] = _atr14(enriched)
    enriched["atr14_median_60"] = enriched["atr14"].rolling(window=60).median()
    enriched["adx14"] = _adx(enriched, window=14)
    return enriched


def _raw_breakout_cross_masks(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    previous_fast = df["ma_fast"].shift(1)
    previous_mid = df["ma_mid"].shift(1)
    long_mask = previous_fast.le(previous_mid) & df["ma_fast"].gt(df["ma_mid"])
    short_mask = previous_fast.ge(previous_mid) & df["ma_fast"].lt(df["ma_mid"])
    return long_mask.fillna(False), short_mask.fillna(False)


def _generate_variant_signals(df: pd.DataFrame, variant: Variant) -> pd.DataFrame:
    if variant.key == "baseline":
        long_mask, short_mask = _raw_breakout_cross_masks(df)
        return _apply_signal_columns(df, long_mask=long_mask, short_mask=short_mask)

    long_mask, short_mask = _raw_breakout_cross_masks(df)

    if variant.min_adx is not None:
        adx_mask = df["adx14"].gt(variant.min_adx)
        long_mask &= adx_mask.fillna(False)
        short_mask &= adx_mask.fillna(False)

    if variant.require_atr_expansion:
        atr_expansion_mask = df["atr14"].gt(df["atr14_median_60"])
        long_mask &= atr_expansion_mask.fillna(False)
        short_mask &= atr_expansion_mask.fillna(False)

    return _apply_signal_columns(df, long_mask=long_mask, short_mask=short_mask)


def _build_trade_frame(signaled: pd.DataFrame, trades: list[Any]) -> pd.DataFrame:
    trade_frame = trades_to_dataframe(trades).copy()
    if trade_frame.empty:
        trade_frame["holding_bars"] = pd.Series(dtype="int64")
        trade_frame["is_win"] = pd.Series(dtype="bool")
        return trade_frame

    index_lookup = {pd.Timestamp(ts): idx for idx, ts in enumerate(signaled.index)}
    trade_frame["entry_time"] = pd.to_datetime(trade_frame["entry_time"])
    trade_frame["exit_time"] = pd.to_datetime(trade_frame["exit_time"])
    trade_frame["entry_bar_index"] = trade_frame["entry_time"].map(index_lookup)
    trade_frame["exit_bar_index"] = trade_frame["exit_time"].map(index_lookup)
    trade_frame["holding_bars"] = trade_frame["exit_bar_index"] - trade_frame["entry_bar_index"]
    trade_frame["is_win"] = trade_frame["net_pnl_ntd"] > 0
    return trade_frame


def _safe_float(value: float | int | None) -> float | None:
    if value is None:
        return None
    value = float(value)
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def _sharpe_display(value: float | None, flag: str | None) -> str:
    if flag == SHARPE_UNSTABLE_FLAG:
        return "不穩定"
    if flag == "insufficient_trades":
        return "樣本不足"
    if value is None:
        return "N/A"
    return f"{value:.2f}"


def _summarize_variant(
    *,
    symbol: str,
    enriched: pd.DataFrame,
    signaled: pd.DataFrame,
    trades: list[Any],
    summary: dict[str, Any],
) -> dict[str, Any]:
    trade_frame = _build_trade_frame(signaled, trades)
    losses = trade_frame[trade_frame["net_pnl_ntd"] < 0]
    same_bar_stop_losses = losses[(losses["exit_reason"] == "stop_loss") & (losses["holding_bars"] == 0)]

    profit_factor = summary.get("profit_factor")
    if isinstance(profit_factor, (float, int)) and math.isinf(float(profit_factor)):
        profit_factor_value: float | None = None
        profit_factor_display = "∞"
    else:
        profit_factor_value = _safe_float(profit_factor)
        profit_factor_display = None

    capital = _assumed_capital(enriched, CONTRACT_SPECS[symbol])
    returns = _trade_returns(trades, capital)
    span_days = (enriched.index[-1] - enriched.index[0]).total_seconds() / 86400.0
    years = max(span_days / 365.25, 1e-6)
    trades_per_year = len(trades) / years if years > 0 else 0.0
    sharpe, sharpe_flag = _sharpe_per_trade(returns, trades_per_year)

    return {
        "n_trades": int(summary.get("total_trades", len(trades))),
        "win_rate": _safe_float(summary.get("win_rate", 0.0)) or 0.0,
        "profit_factor": profit_factor_value,
        "profit_factor_display": profit_factor_display,
        "losing_trades": int(len(losses)),
        "same_bar_stop_loss_loser_count": int(len(same_bar_stop_losses)),
        "same_bar_stop_loss_pct_of_losers": _safe_float(len(same_bar_stop_losses) / len(losses)) if len(losses) else None,
        "total_net_pnl_ntd": _safe_float(summary.get("total_net_pnl_ntd", 0.0)) or 0.0,
        "total_return_pct": _total_return(returns) * 100.0,
        "sharpe_ratio": sharpe,
        "sharpe_flag": sharpe_flag,
        "sharpe_display": _sharpe_display(sharpe, sharpe_flag),
        "max_drawdown_pct": _max_drawdown_pct(returns) * 100.0,
        "trades_per_year_extrapolated": trades_per_year,
    }


def _fmt_num(value: float | int | None, digits: int = 2) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):,.{digits}f}"


def _fmt_pct(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "N/A"
    return f"{value * 100:.{digits}f}%"


def _fmt_pf(metrics: dict[str, Any]) -> str:
    if metrics.get("profit_factor_display") == "∞":
        return "∞"
    return _fmt_num(metrics.get("profit_factor"), 2)


def _markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _table_rows(result_by_variant: dict[str, dict[str, Any]]) -> list[list[str]]:
    rows: list[list[str]] = []
    for variant in VARIANTS:
        metrics = result_by_variant[variant.key]["metrics"]
        rows.append(
            [
                variant.label,
                str(metrics["n_trades"]),
                _fmt_pct(metrics["win_rate"]),
                _fmt_pf(metrics),
                (
                    f"{_fmt_pct(metrics['same_bar_stop_loss_pct_of_losers'])} "
                    f"({metrics['same_bar_stop_loss_loser_count']}/{metrics['losing_trades']})"
                ),
                _fmt_num(metrics["total_net_pnl_ntd"], 0),
                _fmt_num(metrics["total_return_pct"], 2),
                metrics["sharpe_display"],
                _fmt_num(metrics["max_drawdown_pct"], 2),
            ]
        )
    return rows


def _pick_best(result_by_variant: dict[str, dict[str, Any]], metric_name: str) -> tuple[str, dict[str, Any]]:
    best_key = VARIANTS[0].key
    best_payload = result_by_variant[best_key]
    best_value = best_payload["metrics"].get(metric_name)

    def _rank_value(value: Any) -> float:
        if value is None:
            return float("-inf")
        return float(value)

    for variant in VARIANTS[1:]:
        payload = result_by_variant[variant.key]
        value = payload["metrics"].get(metric_name)
        if _rank_value(value) > _rank_value(best_value):
            best_key = variant.key
            best_payload = payload
            best_value = value
    return best_key, best_payload


def _build_report(results: dict[str, dict[str, Any]], dataset_meta: dict[str, Any]) -> str:
    headers = [
        "變體",
        "交易數",
        "勝率",
        "Profit Factor",
        "虧損單中同棒停損占比",
        "總淨損益(NTD)",
        "Total Return (%)",
        "Sharpe Ratio",
        "最大回撤 (%)",
    ]

    lines: list[str] = [
        "# Breakout Regime Filter A/B 測試",
        "",
        "## 1. 實驗設定",
        "- 商品：MTX、TX",
        f"- 資料期間：{dataset_meta['start']} ~ {dataset_meta['end']}",
        f"- 每個商品 K 棒數：約 {dataset_meta['bars_per_symbol']} 根 60 分 K",
        "- 基準策略：5MA / 20MA crossover breakout，訊號於收盤確認、下一根開盤進場",
        f"- 風控：SL={DEFAULT_STRATEGY.breakout_stop_loss_points:.0f} 點、TP={DEFAULT_STRATEGY.breakout_take_profit_points:.0f} 點",
        "- 關鍵指標定義：『虧損單中同棒停損占比』= 虧損單中，`exit_reason=stop_loss` 且 `holding_bars == 0` 的比例（真正同一進場棒就被打掉）",
        "- Regime filter 的正確做法：先在訊號棒上過濾 long_mask / short_mask，再交給 `_apply_signal_columns`，沒有 retroactive slicing",
        f"- Sharpe 判讀門檻：沿用 `{PURE_LUCK_SHARPE_THRESHOLD_SOURCE}` 的純運氣門檻 **{PURE_LUCK_SHARPE_THRESHOLD:.2f}**；若未明顯超過，不視為可信 alpha",
        f"- 樣本警語：{SAMPLE_SIZE_CAVEAT}",
        "",
        "## 2. 變體定義",
    ]

    for variant in VARIANTS:
        lines.append(f"- **{variant.label}**：{variant.description}")

    baseline_summary: dict[str, dict[str, Any]] = {}

    for symbol in ("MTX", "TX"):
        symbol_results = results[symbol]
        baseline_metrics = symbol_results["baseline"]["metrics"]
        baseline_summary[symbol] = baseline_metrics

        lines.extend(
            [
                "",
                f"## 3. {symbol} 結果",
                _markdown_table(headers, _table_rows(symbol_results)),
            ]
        )

    adx20_mtx = results["MTX"]["adx_gt_20"]["metrics"]
    adx20_tx = results["TX"]["adx_gt_20"]["metrics"]
    adx25_mtx = results["MTX"]["adx_gt_25"]["metrics"]
    adx25_tx = results["TX"]["adx_gt_25"]["metrics"]
    atr_mtx = results["MTX"]["atr_expansion"]["metrics"]
    atr_tx = results["TX"]["atr_expansion"]["metrics"]
    combo_mtx = results["MTX"]["adx_gt_20_and_atr_expansion"]["metrics"]
    combo_tx = results["TX"]["adx_gt_20_and_atr_expansion"]["metrics"]
    best_return_mtx_key, best_return_mtx_payload = _pick_best(results["MTX"], "total_return_pct")
    best_return_tx_key, best_return_tx_payload = _pick_best(results["TX"], "total_return_pct")

    def _sharpe_vs_luck(metrics: dict[str, Any]) -> str:
        value = metrics["sharpe_ratio"]
        if value is None:
            return metrics["sharpe_display"]
        return f"{value:.2f}（距離 4.18 尚差 {PURE_LUCK_SHARPE_THRESHOLD - value:.2f}）"

    lines.extend(
        [
            "",
            "## 4. 重點觀察",
            (
                f"- Baseline 與前述診斷一致：MTX {baseline_summary['MTX']['n_trades']} 筆、"
                f"同棒停損占比 {_fmt_pct(baseline_summary['MTX']['same_bar_stop_loss_pct_of_losers'])}；"
                f"TX {baseline_summary['TX']['n_trades']} 筆、同棒停損占比 {_fmt_pct(baseline_summary['TX']['same_bar_stop_loss_pct_of_losers'])}。"
            ),
            (
                f"- **ADX>20 並沒有降低 whipsaw**：MTX 同棒停損占比從 {_fmt_pct(baseline_summary['MTX']['same_bar_stop_loss_pct_of_losers'])} "
                f"升到 {_fmt_pct(adx20_mtx['same_bar_stop_loss_pct_of_losers'])}，TX 也從 {_fmt_pct(baseline_summary['TX']['same_bar_stop_loss_pct_of_losers'])} "
                f"升到 {_fmt_pct(adx20_tx['same_bar_stop_loss_pct_of_losers'])}；Total Return 與 Sharpe 也同步惡化。"
            ),
            (
                f"- **ADX>25 是唯一在 MTX / TX 兩邊都真正降低 same-bar stop-out 的濾網**："
                f"MTX 從 {_fmt_pct(baseline_summary['MTX']['same_bar_stop_loss_pct_of_losers'])} 降到 {_fmt_pct(adx25_mtx['same_bar_stop_loss_pct_of_losers'])}，"
                f"TX 從 {_fmt_pct(baseline_summary['TX']['same_bar_stop_loss_pct_of_losers'])} 降到 {_fmt_pct(adx25_tx['same_bar_stop_loss_pct_of_losers'])}；"
                f"交易數則從 24 壓到 {adx25_mtx['n_trades']} / {adx25_tx['n_trades']} 筆。"
            ),
            (
                f"- **ATR 擴張沒有明顯幫助**：MTX/TX 同棒停損占比只有到 {_fmt_pct(atr_mtx['same_bar_stop_loss_pct_of_losers'])} / "
                f"{_fmt_pct(atr_tx['same_bar_stop_loss_pct_of_losers'])}，仍高於或接近 baseline，且報酬與 Sharpe 都更差。"
            ),
            (
                f"- **組合濾網（ADX>20 + ATR 擴張）最不理想**：MTX/TX 同棒停損占比反而升到 "
                f"{_fmt_pct(combo_mtx['same_bar_stop_loss_pct_of_losers'])} / {_fmt_pct(combo_tx['same_bar_stop_loss_pct_of_losers'])}，"
                f"Sharpe 仍是 {_sharpe_vs_luck(combo_mtx)} / {_sharpe_vs_luck(combo_tx)}。"
            ),
            (
                f"- 以 **Total Return / Sharpe** 看，兩個商品最佳的都是 **{results['MTX'][best_return_mtx_key]['variant_label']} / "
                f"{results['TX'][best_return_tx_key]['variant_label']}**：MTX Total Return {_fmt_num(best_return_mtx_payload['metrics']['total_return_pct'], 2)}%、"
                f"Sharpe {_sharpe_vs_luck(best_return_mtx_payload['metrics'])}；TX Total Return {_fmt_num(best_return_tx_payload['metrics']['total_return_pct'], 2)}%、"
                f"Sharpe {_sharpe_vs_luck(best_return_tx_payload['metrics'])}。"
            ),
            (
                f"- **沒有任何變體接近 4.18 的 Prado 純運氣門檻**：本次最佳 Sharpe 也只有 "
                f"{max(adx25_mtx['sharpe_ratio'], adx25_tx['sharpe_ratio']):.2f} 左右，仍遠低於 4.18，且甚至還是負值。"
            ),
            "",
            "## 5. 結論",
            (
                f"- **是否有明顯降低 same-bar stop-out？** 有，但只有 **ADX>25** 在 MTX/TX 兩邊都明確下降；其他 3 個濾網不是無效，就是更差。"
            ),
            (
                f"- **是否改善 Sharpe / Total Return？** ADX>25 有『小幅改善但仍為負值』：它把虧損收斂一些，但策略整體仍然虧錢，還談不上可用。"
            ),
            (
                f"- **是否接近或超過 4.18 運氣門檻？** 沒有；全部變體都遠低於 {PURE_LUCK_SHARPE_THRESHOLD:.2f}，因此不能宣稱找到可信的新 alpha。"
            ),
            "- 建議：若要延伸研究，只值得把 ADX>25 帶去更長期間 / 樣本外驗證；ATR 擴張與組合濾網目前沒有保留價值。",
        ]
    )
    return "\n".join(lines) + "\n"


def _json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_ready(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def _validate_results(payload: dict[str, Any]) -> None:
    for symbol in ("MTX", "TX"):
        symbol_results = payload["results"][symbol]
        baseline_trades = symbol_results["baseline"]["metrics"]["n_trades"]
        if baseline_trades != 24:
            raise ValueError(f"{symbol} baseline expected 24 trades, got {baseline_trades}.")

        for variant in VARIANTS[1:]:
            trades = symbol_results[variant.key]["metrics"]["n_trades"]
            if trades >= baseline_trades:
                raise ValueError(
                    f"{symbol} variant {variant.key} should reduce trade count below baseline {baseline_trades}, got {trades}."
                )


def run_experiment() -> dict[str, Any]:
    results: dict[str, dict[str, Any]] = {}
    dataset_meta: dict[str, Any] = {}

    for symbol in ("MTX", "TX"):
        enriched = _prepare_enriched(symbol)
        if not dataset_meta:
            dataset_meta = {
                "start": str(enriched.index.min()),
                "end": str(enriched.index.max()),
                "bars_per_symbol": int(len(enriched)),
            }

        symbol_results: dict[str, Any] = {}
        for variant in VARIANTS:
            signaled = _generate_variant_signals(enriched.copy(), variant)
            trades, summary = run_backtest(
                signaled,
                strategy="breakout",
                contract=CONTRACT_SPECS[symbol],
                cost=DEFAULT_COST,
                strategy_cfg=DEFAULT_STRATEGY,
            )
            metrics = _summarize_variant(
                symbol=symbol,
                enriched=enriched,
                signaled=signaled,
                trades=trades,
                summary=summary,
            )
            symbol_results[variant.key] = {
                "variant_key": variant.key,
                "variant_label": variant.label,
                "description": variant.description,
                "metrics": metrics,
            }
        results[symbol] = symbol_results

    payload = {
        "generated_at": pd.Timestamp.now(tz="Asia/Taipei").isoformat(),
        "dataset_meta": dataset_meta,
        "sample_size_caveat": SAMPLE_SIZE_CAVEAT,
        "pure_luck_sharpe_threshold": {
            "value": PURE_LUCK_SHARPE_THRESHOLD,
            "source": PURE_LUCK_SHARPE_THRESHOLD_SOURCE,
        },
        "variants": [
            {
                "key": variant.key,
                "label": variant.label,
                "description": variant.description,
                "min_adx": variant.min_adx,
                "require_atr_expansion": variant.require_atr_expansion,
            }
            for variant in VARIANTS
        ],
        "results": results,
    }
    _validate_results(payload)
    return payload


def write_outputs(payload: dict[str, Any]) -> tuple[Path, Path]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    markdown_path = OUTPUT_DIR / "regime_filter_results.md"
    json_path = OUTPUT_DIR / "regime_filter_results.json"
    report = _build_report(payload["results"], payload["dataset_meta"])
    markdown_path.write_text(report, encoding="utf-8")
    json_path.write_text(json.dumps(_json_ready(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    return markdown_path, json_path


def main() -> None:
    payload = run_experiment()
    markdown_path, json_path = write_outputs(payload)
    print(f"Wrote markdown report: {markdown_path}")
    print(f"Wrote JSON report: {json_path}")


if __name__ == "__main__":
    main()
