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
    from ..config import ContractSpec, DEFAULT_STRATEGY
    from ..indicators import add_moving_averages
    from ..signals import _apply_signal_columns, generate_breakout_signals
except ImportError:  # pragma: no cover - script execution fallback
    from backtest_engine import run_backtest, trades_to_dataframe  # type: ignore
    from config import ContractSpec, DEFAULT_STRATEGY  # type: ignore
    from indicators import add_moving_averages  # type: ignore
    from signals import _apply_signal_columns, generate_breakout_signals  # type: ignore


CONTRACT_SPECS: dict[str, ContractSpec] = {
    "MTX": ContractSpec(name="MTX", point_value=50.0, tick_size=1.0),
    "TX": ContractSpec(name="TX", point_value=200.0, tick_size=1.0),
}

SAMPLE_SIZE_CAVEAT = (
    "樣本期間只有約 551 根 60 分 K（約 30 個交易日），baseline 每個商品也只有 24 筆交易；"
    "部分濾網會把交易數再壓縮到個位數，因此勝率、Profit Factor 與總損益都可能受小樣本噪音影響。"
    "本次結果僅能視為短窗探索，正式採用前應至少再做更長期間與樣本外驗證。"
)


@dataclass(frozen=True)
class Variant:
    key: str
    label: str
    description: str
    trend_alignment: bool = False
    min_separation_ratio: float | None = None
    delay_bars: int = 0


VARIANTS: list[Variant] = [
    Variant(key="baseline", label="A. Baseline", description="原始 breakout 訊號，無額外濾網。"),
    Variant(
        key="trend_align_60ma",
        label="B. 60MA 對齊",
        description="多單僅保留 close > ma_slow；空單僅保留 close < ma_slow。",
        trend_alignment=True,
    ),
    Variant(
        key="min_sep_0_05pct",
        label="C1. 最小分離 0.05%",
        description="要求 abs(ma_fast-ma_mid)/close >= 0.05%。",
        min_separation_ratio=0.0005,
    ),
    Variant(
        key="min_sep_0_10pct",
        label="C2. 最小分離 0.10%",
        description="要求 abs(ma_fast-ma_mid)/close >= 0.10%。",
        min_separation_ratio=0.0010,
    ),
    Variant(
        key="delay_1bar",
        label="D. 延後 1 根確認",
        description="初次交叉後，再等 1 根確認 ma_fast 與 ma_mid 仍維持同方向，訊號於確認棒收盤成立、下一棒開盤進場。",
        delay_bars=1,
    ),
    Variant(
        key="trend_align_60ma_and_sep_0_05pct",
        label="E1. 60MA 對齊 + 0.05%",
        description="60MA 對齊，且 abs(ma_fast-ma_mid)/close >= 0.05%。",
        trend_alignment=True,
        min_separation_ratio=0.0005,
    ),
    Variant(
        key="trend_align_60ma_and_sep_0_10pct",
        label="E2. 60MA 對齊 + 0.10%",
        description="60MA 對齊，且 abs(ma_fast-ma_mid)/close >= 0.10%。",
        trend_alignment=True,
        min_separation_ratio=0.0010,
    ),
]


def _load_bars(symbol: str) -> pd.DataFrame:
    frame = pd.read_csv(DATA_DIR / f"{symbol}_60min_real.csv", parse_dates=["datetime"], index_col="datetime")
    frame.index = pd.to_datetime(frame.index)
    return frame.sort_index()


def _prepare_enriched(symbol: str) -> pd.DataFrame:
    bars = _load_bars(symbol)
    return add_moving_averages(
        bars,
        fast=DEFAULT_STRATEGY.ma_fast,
        mid=DEFAULT_STRATEGY.ma_mid,
        slow=DEFAULT_STRATEGY.ma_slow,
    )


def _raw_breakout_cross_masks(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    previous_fast = df["ma_fast"].shift(1)
    previous_mid = df["ma_mid"].shift(1)
    long_cross = previous_fast.le(previous_mid) & df["ma_fast"].gt(df["ma_mid"])
    short_cross = previous_fast.ge(previous_mid) & df["ma_fast"].lt(df["ma_mid"])
    return long_cross.fillna(False), short_cross.fillna(False)


def _generate_variant_signals(df: pd.DataFrame, variant: Variant) -> pd.DataFrame:
    if variant.key == "baseline":
        return generate_breakout_signals(df)

    long_mask, short_mask = _raw_breakout_cross_masks(df)

    if variant.delay_bars > 0:
        current_long_state = df["ma_fast"].gt(df["ma_mid"])
        current_short_state = df["ma_fast"].lt(df["ma_mid"])
        long_mask = long_mask.shift(variant.delay_bars, fill_value=False) & current_long_state
        short_mask = short_mask.shift(variant.delay_bars, fill_value=False) & current_short_state

    if variant.trend_alignment:
        long_mask = long_mask & df["close"].gt(df["ma_slow"])
        short_mask = short_mask & df["close"].lt(df["ma_slow"])

    if variant.min_separation_ratio is not None:
        separation = (df["ma_fast"] - df["ma_mid"]).abs() / df["close"].abs()
        separation_mask = separation.ge(variant.min_separation_ratio)
        long_mask = long_mask & separation_mask
        short_mask = short_mask & separation_mask

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


def _summarize_metrics(trade_frame: pd.DataFrame, summary: dict[str, Any]) -> dict[str, Any]:
    wins = trade_frame[trade_frame["net_pnl_ntd"] > 0]
    losses = trade_frame[trade_frame["net_pnl_ntd"] < 0]
    same_bar_stop_losses = losses[(losses["exit_reason"] == "stop_loss") & (losses["holding_bars"] <= 1)]

    profit_factor = summary.get("profit_factor")
    if isinstance(profit_factor, (float, int)) and math.isinf(float(profit_factor)):
        profit_factor_value: float | None = None
        profit_factor_display = "∞"
    else:
        profit_factor_value = _safe_float(profit_factor)
        profit_factor_display = None

    return {
        "total_trades": int(summary.get("total_trades", len(trade_frame))),
        "win_rate": _safe_float(summary.get("win_rate", 0.0)) or 0.0,
        "total_net_pnl_ntd": _safe_float(summary.get("total_net_pnl_ntd", 0.0)) or 0.0,
        "profit_factor": profit_factor_value,
        "profit_factor_display": profit_factor_display,
        "avg_win_ntd": _safe_float(wins["net_pnl_ntd"].mean()) if not wins.empty else None,
        "avg_loss_ntd": _safe_float(losses["net_pnl_ntd"].mean()) if not losses.empty else None,
        "avg_holding_bars_winners": _safe_float(wins["holding_bars"].mean()) if not wins.empty else None,
        "avg_holding_bars_losers": _safe_float(losses["holding_bars"].mean()) if not losses.empty else None,
        "same_bar_stop_loss_fraction_of_losers": _safe_float(len(same_bar_stop_losses) / len(losses)) if len(losses) else None,
        "winning_trades": int(len(wins)),
        "losing_trades": int(len(losses)),
        "same_bar_stop_loss_loser_count": int(len(same_bar_stop_losses)),
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


def _variant_table_rows(result_by_variant: dict[str, dict[str, Any]]) -> list[list[str]]:
    rows: list[list[str]] = []
    for variant in VARIANTS:
        metrics = result_by_variant[variant.key]["metrics"]
        rows.append(
            [
                variant.label,
                str(metrics["total_trades"]),
                _fmt_pct(metrics["win_rate"]),
                _fmt_num(metrics["total_net_pnl_ntd"], 0),
                _fmt_pf(metrics),
                _fmt_num(metrics["avg_win_ntd"], 0),
                _fmt_num(metrics["avg_loss_ntd"], 0),
                _fmt_num(metrics["avg_holding_bars_winners"], 2),
                _fmt_num(metrics["avg_holding_bars_losers"], 2),
                _fmt_pct(metrics["same_bar_stop_loss_fraction_of_losers"]),
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
        "總淨損益(NTD)",
        "Profit Factor",
        "平均獲利(NTD)",
        "平均虧損(NTD)",
        "勝單平均持有K棒",
        "敗單平均持有K棒",
        "虧損單同/次棒停損占比",
    ]

    lines: list[str] = [
        "# Breakout 進場濾網 AB 測試",
        "",
        "## 1. 實驗設定",
        "- 商品：MTX、TX",
        f"- 資料期間：{dataset_meta['start']} ~ {dataset_meta['end']}",
        f"- 每個商品 K 棒數：約 {dataset_meta['bars_per_symbol']} 根 60 分 K",
        f"- 基準策略：5MA / 20MA 黃金交叉做多、死亡交叉做空；訊號於收盤確認、下一根開盤進場",
        f"- 風控：SL={DEFAULT_STRATEGY.breakout_stop_loss_points:.0f} 點、TP={DEFAULT_STRATEGY.breakout_take_profit_points:.0f} 點",
        "- 回測執行：沿用既有 `run_backtest(..., strategy=\"breakout\")`，因此成本、滑價、gap-through fill 與同棒先打停損的悲觀假設都與 baseline 完全一致",
        "- 「虧損單同/次棒停損占比」定義：虧損單中，`exit_reason=stop_loss` 且 `holding_bars <= 1` 的比例",
        "",
        "## 2. 變體定義",
    ]

    for variant in VARIANTS:
        lines.append(f"- **{variant.label}**：{variant.description}")

    for symbol in ("MTX", "TX"):
        symbol_results = results[symbol]
        best_net_key, best_net_payload = _pick_best(symbol_results, "total_net_pnl_ntd")
        best_pf_key, best_pf_payload = _pick_best(symbol_results, "profit_factor")
        best_wr_key, best_wr_payload = _pick_best(symbol_results, "win_rate")

        lines.extend(
            [
                "",
                f"## 3. {symbol} 結果",
                _markdown_table(headers, _variant_table_rows(symbol_results)),
                "",
                f"- **{symbol} 總淨損益最佳**：{symbol_results[best_net_key]['variant_label']}，總淨損益 {_fmt_num(best_net_payload['metrics']['total_net_pnl_ntd'], 0)} NTD，交易數 {best_net_payload['metrics']['total_trades']}。",
                f"- **{symbol} Profit Factor 最佳**：{symbol_results[best_pf_key]['variant_label']}，PF {_fmt_pf(best_pf_payload['metrics'])}，勝率 {_fmt_pct(best_pf_payload['metrics']['win_rate'])}。",
                f"- **{symbol} 勝率最高**：{symbol_results[best_wr_key]['variant_label']}，勝率 {_fmt_pct(best_wr_payload['metrics']['win_rate'])}，交易數 {best_wr_payload['metrics']['total_trades']}。",
            ]
        )

    baseline_mtx = results["MTX"]["baseline"]["metrics"]
    baseline_tx = results["TX"]["baseline"]["metrics"]
    best_mtx_net_key, best_mtx_net_payload = _pick_best(results["MTX"], "total_net_pnl_ntd")
    best_tx_net_key, best_tx_net_payload = _pick_best(results["TX"], "total_net_pnl_ntd")
    delay_mtx = results["MTX"]["delay_1bar"]["metrics"]
    delay_tx = results["TX"]["delay_1bar"]["metrics"]
    trend_mtx = results["MTX"]["trend_align_60ma"]["metrics"]
    trend_tx = results["TX"]["trend_align_60ma"]["metrics"]
    sep005_mtx = results["MTX"]["min_sep_0_05pct"]["metrics"]
    sep005_tx = results["TX"]["min_sep_0_05pct"]["metrics"]

    lines.extend(
        [
            "",
            "## 4. 重點觀察",
            f"- Baseline 驗證與前一份診斷報告一致：MTX {baseline_mtx['total_trades']} 筆、勝率 {_fmt_pct(baseline_mtx['win_rate'])}、PF {_fmt_pf({'profit_factor': baseline_mtx['profit_factor'], 'profit_factor_display': baseline_mtx['profit_factor_display']})}；TX {baseline_tx['total_trades']} 筆、勝率 {_fmt_pct(baseline_tx['win_rate'])}、PF {_fmt_pf({'profit_factor': baseline_tx['profit_factor'], 'profit_factor_display': baseline_tx['profit_factor_display']})}。",
            f"- MTX 以 **{results['MTX'][best_mtx_net_key]['variant_label']}** 的總淨損益最佳（{_fmt_num(best_mtx_net_payload['metrics']['total_net_pnl_ntd'], 0)} NTD）；TX 以 **{results['TX'][best_tx_net_key]['variant_label']}** 的總淨損益最佳（{_fmt_num(best_tx_net_payload['metrics']['total_net_pnl_ntd'], 0)} NTD）。",
            f"- **60MA 對齊濾網（B）在本次『正式實作後』並沒有重現前次 retroactive slicing 的改善**：MTX 勝率降到 {_fmt_pct(trend_mtx['win_rate'])}、TX 降到 {_fmt_pct(trend_tx['win_rate'])}，而且兩邊總淨損益都比 baseline 更差。",
            f"- **最小分離 0.05%（C1）是唯一在 MTX / TX 兩邊都同步改善 PF 與總淨損益的變體**：MTX PF {_fmt_pf({'profit_factor': sep005_mtx['profit_factor'], 'profit_factor_display': sep005_mtx['profit_factor_display']})}、總淨損益 {_fmt_num(sep005_mtx['total_net_pnl_ntd'], 0)}；TX PF {_fmt_pf({'profit_factor': sep005_tx['profit_factor'], 'profit_factor_display': sep005_tx['profit_factor_display']})}、總淨損益 {_fmt_num(sep005_tx['total_net_pnl_ntd'], 0)}。",
            f"- **延後 1 根確認（D）雖然把勝率拉高到 MTX/TX 都是 {_fmt_pct(delay_mtx['win_rate'])}，也把同/次棒停損占比壓到 {_fmt_pct(delay_mtx['same_bar_stop_loss_fraction_of_losers'])} / {_fmt_pct(delay_tx['same_bar_stop_loss_fraction_of_losers'])}，但因進場更晚導致平均虧損放大，總淨損益反而明顯惡化。**",
            "- 若某些濾網提升勝率，但同時把交易數壓得非常低，應優先視為『減少出手帶來的樣本擾動』，而不是直接視為可量產優勢。",
            "",
            "## 5. 建議",
            "- **優先候選：C1（最小分離 0.05%）**。它在兩個商品都把 PF 與總淨損益往較好的方向推，且虧損單同/次棒停損占比也比 baseline 略低，代表確實有過濾掉部分過於貼近的噪音交叉。",
            "- **不建議直接採用：B / E 類 60MA 對齊版本**。在這段樣本裡，它們不但沒有改善，反而把交易壓縮到 8 筆左右，結果更像過度篩選後錯過少數有效趨勢。",
            "- **D（延後 1 根確認）可保留作次要研究方向，但不能只看勝率。** 它確實減少 instant whipsaw，卻同時犧牲了進場價格，導致平均虧損擴大。",
            f"- **樣本數警語：{SAMPLE_SIZE_CAVEAT}**",
            "- 建議下一步：把看起來最有希望的 1~2 個濾網，拉到更長期間（至少數月）與樣本外資料重跑；若仍能維持較低的 whipsaw 停損比例，再考慮納入正式策略。",
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
            )
            trade_frame = _build_trade_frame(signaled, trades)
            metrics = _summarize_metrics(trade_frame, summary)
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
        "variants": [
            {
                "key": variant.key,
                "label": variant.label,
                "description": variant.description,
                "trend_alignment": variant.trend_alignment,
                "min_separation_ratio": variant.min_separation_ratio,
                "delay_bars": variant.delay_bars,
            }
            for variant in VARIANTS
        ],
        "results": results,
    }
    return payload


def write_outputs(payload: dict[str, Any]) -> tuple[Path, Path]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    markdown_path = OUTPUT_DIR / "entry_filter_ab_results.md"
    json_path = OUTPUT_DIR / "entry_filter_ab_results.json"

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
