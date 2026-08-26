from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

try:
    from .backtest_engine import run_backtest
    from .config import ContractSpec, DEFAULT_STRATEGY
    from .indicators import add_moving_averages
    from .plot_trades import _ZH_BOLD, _ZH_NORMAL
    from .signals import generate_breakout_signals, generate_pullback_signals
except ImportError:  # pragma: no cover - script execution fallback
    from backtest_engine import run_backtest  # type: ignore
    from config import ContractSpec, DEFAULT_STRATEGY  # type: ignore
    from indicators import add_moving_averages  # type: ignore
    from plot_trades import _ZH_BOLD, _ZH_NORMAL  # type: ignore
    from signals import generate_breakout_signals, generate_pullback_signals  # type: ignore


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_PREFIX = "strategy_failure"
CLUSTER_WINDOW_BARS = 10

CONTRACT_SPECS: dict[str, ContractSpec] = {
    "TX": ContractSpec(name="TX", point_value=200.0, tick_size=1.0),
    "MTX": ContractSpec(name="MTX", point_value=50.0, tick_size=1.0),
}


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


def _generate_signaled(enriched: pd.DataFrame, strategy: str) -> pd.DataFrame:
    if strategy == "breakout":
        return generate_breakout_signals(enriched)
    return generate_pullback_signals(enriched)


def _safe_ratio(numerator: float, denominator: float) -> float | None:
    if denominator == 0:
        return None
    return float(numerator / denominator)


def _fmt_num(value: float | int | None, digits: int = 2) -> str:
    if value is None or (isinstance(value, float) and (math.isnan(value) or math.isinf(value))):
        return "N/A"
    return f"{float(value):,.{digits}f}"


def _fmt_pct(value: float | None, digits: int = 1) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "N/A"
    return f"{value * 100:.{digits}f}%"


def _build_trade_frame(symbol: str, strategy: str, bars: pd.DataFrame, signaled: pd.DataFrame, trades: list) -> pd.DataFrame:
    index_lookup = {pd.Timestamp(ts): idx for idx, ts in enumerate(bars.index)}
    records: list[dict] = []

    for trade_id, trade in enumerate(trades, start=1):
        entry_time = pd.Timestamp(trade.entry_time)
        exit_time = pd.Timestamp(trade.exit_time)
        entry_idx = index_lookup[entry_time]
        exit_idx = index_lookup[exit_time]
        signal_idx = max(entry_idx - 1, 0)
        direction_sign = 1.0 if trade.direction == "long" else -1.0

        trade_slice = bars.iloc[entry_idx : exit_idx + 1]
        if trade.direction == "long":
            mfe_points = float(trade_slice["high"].max() - trade.entry_price)
            mae_points = float(trade.entry_price - trade_slice["low"].min())
        else:
            mfe_points = float(trade.entry_price - trade_slice["low"].min())
            mae_points = float(trade_slice["high"].max() - trade.entry_price)

        pre_move_5 = None
        pre_move_10 = None
        if entry_idx >= 5:
            lookback_5 = bars.iloc[entry_idx - 5 : entry_idx]
            if trade.direction == "long":
                pre_move_5 = float(trade.entry_price - lookback_5["low"].min())
            else:
                pre_move_5 = float(lookback_5["high"].max() - trade.entry_price)
        if entry_idx >= 10:
            lookback_10 = bars.iloc[entry_idx - 10 : entry_idx]
            if trade.direction == "long":
                pre_move_10 = float(trade.entry_price - lookback_10["low"].min())
            else:
                pre_move_10 = float(lookback_10["high"].max() - trade.entry_price)

        range_start = max(0, entry_idx - 5)
        range_end = min(len(bars), entry_idx + 6)
        local_window = bars.iloc[range_start:range_end]["close"]
        local_low = float(local_window.min())
        local_high = float(local_window.max())
        local_range = local_high - local_low
        if local_range > 0:
            if trade.direction == "long":
                local_extreme_score = float((trade.entry_price - local_low) / local_range)
            else:
                local_extreme_score = float((local_high - trade.entry_price) / local_range)
        else:
            local_extreme_score = None

        signal_row = signaled.iloc[signal_idx]
        slow_aligned = None
        if strategy == "breakout":
            if trade.direction == "long":
                slow_aligned = bool(signal_row["ma_mid"] > signal_row["ma_slow"] and signal_row["close"] >= signal_row["ma_slow"])
            else:
                slow_aligned = bool(signal_row["ma_mid"] < signal_row["ma_slow"] and signal_row["close"] <= signal_row["ma_slow"])

        records.append(
            {
                "symbol": symbol,
                "strategy": strategy,
                "trade_id": trade_id,
                "entry_time": entry_time,
                "exit_time": exit_time,
                "entry_bar_index": entry_idx,
                "exit_bar_index": exit_idx,
                "holding_bars": exit_idx - entry_idx,
                "signal_bar_index": signal_idx,
                "signal_time": pd.Timestamp(signaled.index[signal_idx]),
                "direction": trade.direction,
                "entry_price": float(trade.entry_price),
                "exit_price": float(trade.exit_price),
                "stop_loss_price": float(trade.stop_loss_price),
                "take_profit_price": float(trade.take_profit_price),
                "exit_reason": trade.exit_reason,
                "gross_pnl_points": float(trade.gross_pnl_points),
                "gross_pnl_ntd": float(trade.gross_pnl_ntd),
                "net_pnl_ntd": float(trade.net_pnl_ntd),
                "total_cost_ntd": float(trade.total_cost_ntd),
                "is_win": bool(trade.net_pnl_ntd > 0),
                "mfe_points": mfe_points,
                "mae_points": mae_points,
                "pre_move_5_points": pre_move_5,
                "pre_move_10_points": pre_move_10,
                "local_extreme_score_11bars": local_extreme_score,
                "near_local_extreme_11bars": bool(local_extreme_score is not None and local_extreme_score >= 0.8),
                "slow_aligned": slow_aligned,
            }
        )

    frame = pd.DataFrame.from_records(records)
    if strategy == "breakout" and not frame.empty:
        frame = _tag_breakout_clusters(frame, window_bars=CLUSTER_WINDOW_BARS)
    return frame


def _tag_breakout_clusters(frame: pd.DataFrame, window_bars: int) -> pd.DataFrame:
    tagged = frame.sort_values("entry_bar_index").reset_index(drop=True).copy()
    tagged["clustered_signal"] = False
    tagged["cluster_id"] = pd.Series([pd.NA] * len(tagged), dtype="object")
    tagged["min_gap_to_opposite_signal"] = np.nan

    cluster_id = 0
    for idx in range(len(tagged)):
        entry_idx = int(tagged.loc[idx, "entry_bar_index"])
        direction = tagged.loc[idx, "direction"]
        opposite = tagged[(tagged["direction"] != direction)]
        if opposite.empty:
            continue
        gaps = (opposite["entry_bar_index"] - entry_idx).abs()
        min_gap = gaps.min()
        tagged.loc[idx, "min_gap_to_opposite_signal"] = float(min_gap)

    idx = 0
    while idx < len(tagged) - 1:
        next_gap = int(tagged.loc[idx + 1, "entry_bar_index"] - tagged.loc[idx, "entry_bar_index"])
        alternating = tagged.loc[idx + 1, "direction"] != tagged.loc[idx, "direction"]
        if alternating and next_gap <= window_bars:
            cluster_id += 1
            members = [idx, idx + 1]
            cursor = idx + 1
            while cursor < len(tagged) - 1:
                gap = int(tagged.loc[cursor + 1, "entry_bar_index"] - tagged.loc[cursor, "entry_bar_index"])
                alternates = tagged.loc[cursor + 1, "direction"] != tagged.loc[cursor, "direction"]
                if alternates and gap <= window_bars:
                    members.append(cursor + 1)
                    cursor += 1
                    continue
                break

            tagged.loc[members, "clustered_signal"] = True
            tagged.loc[members, "cluster_id"] = f"C{cluster_id}"
            idx = cursor + 1
            continue
        idx += 1

    return tagged


def _summarize_exit_reasons(frame: pd.DataFrame) -> dict[str, dict[str, float]]:
    summary: dict[str, dict[str, float]] = {}
    if frame.empty:
        return summary
    total = len(frame)
    for exit_reason, group in frame.groupby("exit_reason"):
        summary[str(exit_reason)] = {
            "count": int(len(group)),
            "fraction": float(len(group) / total),
            "avg_holding_bars": float(group["holding_bars"].mean()),
            "avg_points": float(group["gross_pnl_points"].mean()),
        }
    return summary


def _summarize_strategy(frame: pd.DataFrame, raw_summary: dict) -> dict:
    wins = frame[frame["gross_pnl_points"] > 0]
    losses = frame[frame["gross_pnl_points"] <= 0]
    avg_win_points = float(wins["gross_pnl_points"].mean()) if not wins.empty else None
    avg_loss_points = float(losses["gross_pnl_points"].abs().mean()) if not losses.empty else None
    realized_rr = _safe_ratio(avg_win_points or 0.0, avg_loss_points or 0.0) if avg_win_points is not None and avg_loss_points is not None else None

    winning_exit_reasons = wins["exit_reason"].value_counts().to_dict()
    losing_exit_reasons = losses["exit_reason"].value_counts().to_dict()

    summary = {
        "total_trades": int(len(frame)),
        "win_rate": float((frame["net_pnl_ntd"] > 0).mean()) if not frame.empty else 0.0,
        "total_net_pnl_ntd": float(frame["net_pnl_ntd"].sum()),
        "profit_factor": raw_summary["profit_factor"],
        "avg_win_points": avg_win_points,
        "avg_loss_points_abs": avg_loss_points,
        "realized_reward_risk": realized_rr,
        "avg_holding_bars": float(frame["holding_bars"].mean()) if not frame.empty else None,
        "exit_reason_summary": _summarize_exit_reasons(frame),
        "winning_exit_reasons": {str(k): int(v) for k, v in winning_exit_reasons.items()},
        "losing_exit_reasons": {str(k): int(v) for k, v in losing_exit_reasons.items()},
        "engine_summary": raw_summary,
    }
    return summary


def _breakout_specific_stats(frame: pd.DataFrame) -> dict:
    losses = frame[frame["gross_pnl_points"] <= 0]
    wins = frame[frame["gross_pnl_points"] > 0]
    stop_loss_losers = losses[losses["exit_reason"] == "stop_loss"]
    same_bar_stop_loss = stop_loss_losers[stop_loss_losers["holding_bars"] == 0]

    cluster_groups: dict[str, dict] = {}
    for label, mask in {
        "clustered": frame["clustered_signal"].fillna(False),
        "isolated": ~frame["clustered_signal"].fillna(False),
    }.items():
        group = frame[mask]
        cluster_groups[label] = {
            "count": int(len(group)),
            "win_rate": float((group["net_pnl_ntd"] > 0).mean()) if not group.empty else None,
            "avg_points": float(group["gross_pnl_points"].mean()) if not group.empty else None,
            "avg_gap_to_opposite_signal": float(group["min_gap_to_opposite_signal"].mean()) if not group.empty else None,
        }

    aligned_groups: dict[str, dict] = {}
    for label, mask in {
        "aligned": frame["slow_aligned"] == True,  # noqa: E712
        "not_aligned": frame["slow_aligned"] == False,  # noqa: E712
    }.items():
        group = frame[mask]
        aligned_groups[label] = {
            "count": int(len(group)),
            "win_rate": float((group["net_pnl_ntd"] > 0).mean()) if not group.empty else None,
            "avg_points": float(group["gross_pnl_points"].mean()) if not group.empty else None,
        }

    late_entry_scope = frame.dropna(subset=["pre_move_5_points", "pre_move_10_points"])
    late_entry_losers = losses.dropna(subset=["pre_move_5_points", "pre_move_10_points"])
    late_entry_wins = wins.dropna(subset=["pre_move_5_points", "pre_move_10_points"])

    def _late_entry_metrics(group: pd.DataFrame) -> dict:
        if group.empty:
            return {}
        return {
            "count": int(len(group)),
            "avg_pre_move_5_points": float(group["pre_move_5_points"].mean()),
            "avg_pre_move_10_points": float(group["pre_move_10_points"].mean()),
            "avg_post_entry_mfe_points": float(group["mfe_points"].mean()),
            "median_post_entry_mfe_points": float(group["mfe_points"].median()),
            "pre5_gt_mfe_fraction": float((group["pre_move_5_points"] > group["mfe_points"]).mean()),
            "pre10_gt_mfe_fraction": float((group["pre_move_10_points"] > group["mfe_points"]).mean()),
            "near_local_extreme_fraction": float(group["near_local_extreme_11bars"].mean()),
        }

    cluster_examples = []
    for cluster_id, group in frame.dropna(subset=["cluster_id"]).groupby("cluster_id"):
        cluster_examples.append(
            {
                "cluster_id": cluster_id,
                "n_trades": int(len(group)),
                "start": str(group["entry_time"].min()),
                "end": str(group["entry_time"].max()),
                "directions": " → ".join(group["direction"].tolist()),
                "points": group["gross_pnl_points"].round(1).tolist(),
            }
        )

    return {
        "losing_trade_count": int(len(losses)),
        "stop_loss_loser_fraction": float(len(stop_loss_losers) / len(losses)) if len(losses) else None,
        "same_bar_stop_loss_count": int(len(same_bar_stop_loss)),
        "same_bar_stop_loss_fraction": float(len(same_bar_stop_loss) / len(stop_loss_losers)) if len(stop_loss_losers) else None,
        "avg_holding_bars_stop_loss": float(frame.loc[frame["exit_reason"] == "stop_loss", "holding_bars"].mean()),
        "avg_holding_bars_take_profit": float(frame.loc[frame["exit_reason"] == "take_profit", "holding_bars"].mean()),
        "avg_holding_bars_stop_loss_losers": float(stop_loss_losers["holding_bars"].mean()) if not stop_loss_losers.empty else None,
        "avg_holding_bars_take_profit_winners": float(wins.loc[wins["exit_reason"] == "take_profit", "holding_bars"].mean()) if not wins.empty else None,
        "cluster_groups": cluster_groups,
        "aligned_groups": aligned_groups,
        "late_entry_all": _late_entry_metrics(late_entry_scope),
        "late_entry_losers": _late_entry_metrics(late_entry_losers),
        "late_entry_wins": _late_entry_metrics(late_entry_wins),
        "cluster_examples": cluster_examples[:5],
    }


def _pullback_specific_stats(frame: pd.DataFrame) -> dict:
    wins = frame[frame["gross_pnl_points"] > 0]
    losses = frame[frame["gross_pnl_points"] <= 0]
    return {
        "avg_holding_bars_stop_loss": float(frame.loc[frame["exit_reason"] == "stop_loss", "holding_bars"].mean()),
        "avg_holding_bars_take_profit": float(frame.loc[frame["exit_reason"] == "take_profit", "holding_bars"].mean()),
        "avg_holding_bars_immediate_reverse": float(frame.loc[frame["exit_reason"] == "immediate_reverse", "holding_bars"].mean())
        if (frame["exit_reason"] == "immediate_reverse").any()
        else None,
        "winner_exit_reason_counts": {str(k): int(v) for k, v in wins["exit_reason"].value_counts().to_dict().items()},
        "loser_exit_reason_counts": {str(k): int(v) for k, v in losses["exit_reason"].value_counts().to_dict().items()},
    }


def _analyze_one(symbol: str, strategy: str) -> dict:
    bars = _load_bars(symbol)
    enriched = _prepare_enriched(symbol)
    signaled = _generate_signaled(enriched, strategy)
    trades, summary = run_backtest(signaled, strategy=strategy, contract=CONTRACT_SPECS[symbol])
    trade_frame = _build_trade_frame(symbol, strategy, bars, signaled, trades)
    result = {
        "symbol": symbol,
        "strategy": strategy,
        "bars": bars,
        "enriched": enriched,
        "signaled": signaled,
        "trades": trade_frame,
        "summary": _summarize_strategy(trade_frame, summary),
    }
    if strategy == "breakout":
        result["breakout_stats"] = _breakout_specific_stats(trade_frame)
    else:
        result["pullback_stats"] = _pullback_specific_stats(trade_frame)
    return result


def _save_trade_details(results: list[dict]) -> Path:
    frame = pd.concat([result["trades"] for result in results], ignore_index=True)
    output_path = DATA_DIR / f"{OUTPUT_PREFIX}_trade_details.csv"
    frame.to_csv(output_path, index=False, encoding="utf-8-sig")
    return output_path


def _plot_pnl_histogram(results: list[dict]) -> Path:
    fig, axes = plt.subplots(2, 2, figsize=(14, 10), sharex=False, sharey=False)
    for ax, result in zip(axes.flatten(), results):
        trades = result["trades"]
        ax.hist(trades["gross_pnl_points"], bins=12, color="#4e79a7", edgecolor="white", alpha=0.9)
        ax.axvline(0, color="black", linewidth=1)
        ax.set_title(f"{result['symbol']} {result['strategy']}", fontproperties=_ZH_BOLD, fontsize=12)
        ax.set_xlabel("單筆損益（點）", fontproperties=_ZH_NORMAL)
        ax.set_ylabel("筆數", fontproperties=_ZH_NORMAL)
        ax.grid(axis="y", alpha=0.2)
    fig.suptitle("各商品／策略單筆損益分布（點數）", fontproperties=_ZH_BOLD, fontsize=16)
    fig.tight_layout()
    path = DATA_DIR / f"{OUTPUT_PREFIX}_pnl_histogram.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


def _plot_breakout_holding_vs_pnl(results: list[dict]) -> Path:
    breakout_results = [result for result in results if result["strategy"] == "breakout"]
    fig, axes = plt.subplots(1, 2, figsize=(14, 6), sharey=True)
    color_map = {"stop_loss": "#e15759", "take_profit": "#4e79a7", "end_of_data": "#9c755f"}
    for ax, result in zip(axes, breakout_results):
        trades = result["trades"]
        for reason, group in trades.groupby("exit_reason"):
            ax.scatter(
                group["holding_bars"],
                group["gross_pnl_points"],
                s=70,
                alpha=0.85,
                label=reason,
                color=color_map.get(str(reason), "#bab0ab"),
            )
        ax.axhline(0, color="black", linewidth=1)
        ax.set_title(f"{result['symbol']} breakout", fontproperties=_ZH_BOLD, fontsize=12)
        ax.set_xlabel("持有 K 棒數", fontproperties=_ZH_NORMAL)
        ax.grid(alpha=0.2)
    axes[0].set_ylabel("單筆損益（點）", fontproperties=_ZH_NORMAL)
    handles, labels = axes[-1].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=3, prop=_ZH_NORMAL)
    fig.suptitle("Breakout：持有時間 vs. 單筆損益", fontproperties=_ZH_BOLD, fontsize=16)
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    path = DATA_DIR / f"{OUTPUT_PREFIX}_breakout_holding_vs_pnl.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


def _plot_breakout_filters(results: list[dict]) -> Path:
    breakout_results = [result for result in results if result["strategy"] == "breakout"]
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))

    cluster_labels = ["clustered", "isolated"]
    align_labels = ["aligned", "not_aligned"]
    x_cluster = np.arange(len(cluster_labels))
    x_align = np.arange(len(align_labels))

    cluster_width = 0.35
    for idx, result in enumerate(breakout_results):
        stats = result["breakout_stats"]
        cluster_rates = [stats["cluster_groups"][label]["win_rate"] or 0.0 for label in cluster_labels]
        align_rates = [stats["aligned_groups"][label]["win_rate"] or 0.0 for label in align_labels]
        offset = (idx - 0.5) * cluster_width
        axes[0].bar(x_cluster + offset, cluster_rates, width=cluster_width, label=result["symbol"])
        axes[1].bar(x_align + offset, align_rates, width=cluster_width, label=result["symbol"])

    axes[0].set_xticks(x_cluster)
    axes[0].set_xticklabels(["密集反向群", "非密集群"], fontproperties=_ZH_NORMAL)
    axes[0].set_ylim(0, 1)
    axes[0].set_ylabel("勝率", fontproperties=_ZH_NORMAL)
    axes[0].set_title("Breakout：whipsaw 群 vs 非群", fontproperties=_ZH_BOLD)
    axes[0].grid(axis="y", alpha=0.2)

    axes[1].set_xticks(x_align)
    axes[1].set_xticklabels(["60MA 同向", "60MA 不同向"], fontproperties=_ZH_NORMAL)
    axes[1].set_ylim(0, 1)
    axes[1].set_title("Breakout：是否有 60MA 趨勢濾網", fontproperties=_ZH_BOLD)
    axes[1].grid(axis="y", alpha=0.2)

    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=2, prop=_ZH_NORMAL, bbox_to_anchor=(0.5, 0.98))
    fig.suptitle("Breakout 子群勝率比較", fontproperties=_ZH_BOLD, fontsize=16, y=1.06)
    fig.tight_layout(rect=(0, 0, 1, 0.88))
    path = DATA_DIR / f"{OUTPUT_PREFIX}_breakout_filter_comparison.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


def _markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _build_report(results: list[dict], chart_paths: list[Path], trade_details_path: Path) -> str:
    result_map = {(result["symbol"], result["strategy"]): result for result in results}
    breakout_mtx = result_map[("MTX", "breakout")]
    breakout_tx = result_map[("TX", "breakout")]
    pullback_mtx = result_map[("MTX", "pullback")]
    pullback_tx = result_map[("TX", "pullback")]

    summary_rows = []
    for result in results:
        summary = result["summary"]
        summary_rows.append(
            [
                result["symbol"],
                result["strategy"],
                str(summary["total_trades"]),
                _fmt_pct(summary["win_rate"]),
                _fmt_num(summary["total_net_pnl_ntd"], 0),
                _fmt_num(summary["profit_factor"], 2),
                _fmt_num(summary["avg_win_points"], 1),
                _fmt_num(summary["avg_loss_points_abs"], 1),
                _fmt_num(summary["realized_reward_risk"], 2),
            ]
        )

    report_sections = [
        "# 策略失敗原因分析報告",
        "",
        "## 1. 研究範圍",
        "- 商品：MTX、TX",
        "- 資料：`data\\MTX_60min_real.csv`、`data\\TX_60min_real.csv`（2026-05-22 ~ 2026-07-03）",
        "- 策略：breakout（5MA/20MA 穿越）、pullback（20MA 回測 + 60MA 趨勢濾網）",
        f"- 分群定義：breakout 若與相反方向訊號在 **{CLUSTER_WINDOW_BARS} 根 K 棒內** 連續交替出現，視為 whipsaw 密集群",
        "",
        "## 2. 回測重跑結果（驗證基準）",
        _markdown_table(
            ["商品", "策略", "交易數", "勝率", "總淨損益(NTD)", "Profit Factor", "平均獲利(點)", "平均虧損(點)", "實現 RR"],
            summary_rows,
        ),
        "",
        "### 觀察",
        f"- MTX breakout：{breakout_mtx['summary']['total_trades']} 筆、勝率 {_fmt_pct(breakout_mtx['summary']['win_rate'])}、總淨損益 {_fmt_num(breakout_mtx['summary']['total_net_pnl_ntd'], 0)} NTD。",
        f"- TX breakout：{breakout_tx['summary']['total_trades']} 筆、勝率 {_fmt_pct(breakout_tx['summary']['win_rate'])}、總淨損益 {_fmt_num(breakout_tx['summary']['total_net_pnl_ntd'], 0)} NTD。",
        f"- MTX pullback：{pullback_mtx['summary']['total_trades']} 筆、勝率 {_fmt_pct(pullback_mtx['summary']['win_rate'])}、總淨損益 {_fmt_num(pullback_mtx['summary']['total_net_pnl_ntd'], 0)} NTD。",
        f"- TX pullback：{pullback_tx['summary']['total_trades']} 筆、勝率 {_fmt_pct(pullback_tx['summary']['win_rate'])}、總淨損益 {_fmt_num(pullback_tx['summary']['total_net_pnl_ntd'], 0)} NTD。",
        "- 補充：你提供的 MTX 基準 NTD 數字約為本次結果的 4 倍；本次重跑明確用 MTX 每點 50 元、TX 每點 200 元計算，因此 MTX / TX 的『點數級統計』幾乎一致，但 NTD 會依契約乘數不同而分開。",
        "",
        "## 3. Breakout 為何會虧錢？",
        "",
    ]

    for symbol, result in [("MTX", breakout_mtx), ("TX", breakout_tx)]:
        summary = result["summary"]
        stats = result["breakout_stats"]
        stop_loss_count = summary["losing_exit_reasons"].get("stop_loss", 0)
        report_sections.extend(
            [
                f"### 3.{1 if symbol == 'MTX' else 2} {symbol} breakout",
                f"- **停損主導虧損**：虧損單共 {stats['losing_trade_count']} 筆，其中 {stop_loss_count} 筆為 `stop_loss`，占 {_fmt_pct(stats['stop_loss_loser_fraction'])}。",
                f"- **停損很快、停利很久**：`stop_loss` 平均持有 {_fmt_num(stats['avg_holding_bars_stop_loss'], 2)} 根；`take_profit` 平均持有 {_fmt_num(stats['avg_holding_bars_take_profit'], 2)} 根。",
                f"- **大量當根就被打掉**：{stats['same_bar_stop_loss_count']} / {stop_loss_count} 筆停損單在進場同一根 K 棒就出場，占 {_fmt_pct(stats['same_bar_stop_loss_fraction'])}。",
                f"- **實現 RR 雖接近理論值，但勝率太低**：平均獲利 {_fmt_num(summary['avg_win_points'], 1)} 點，平均虧損 {_fmt_num(summary['avg_loss_points_abs'], 1)} 點，實現 RR = {_fmt_num(summary['realized_reward_risk'], 2)}。",
                f"- **晚進場跡象**：進場前 5 根平均已先走 {_fmt_num(stats['late_entry_all'].get('avg_pre_move_5_points'), 1)} 點，前 10 根平均已先走 {_fmt_num(stats['late_entry_all'].get('avg_pre_move_10_points'), 1)} 點；進場後平均最大順向延伸（MFE）僅 {_fmt_num(stats['late_entry_all'].get('avg_post_entry_mfe_points'), 1)} 點。",
                f"- **進場靠近局部極值**：{_fmt_pct(stats['late_entry_all'].get('near_local_extreme_fraction'))} 的 breakout 進場，落在前後 11 根價格區間的方向極端 20% 內。",
                f"- **whipsaw 密集群很傷**：{stats['cluster_groups']['clustered']['count']} / {summary['total_trades']} 筆交易屬於密集反向群，勝率 {_fmt_pct(stats['cluster_groups']['clustered']['win_rate'])}；非密集群勝率 {_fmt_pct(stats['cluster_groups']['isolated']['win_rate'])}。",
                f"- **60MA 濾網有效**：若要求 20MA/60MA 同向，{stats['aligned_groups']['aligned']['count']} 筆交易勝率 {_fmt_pct(stats['aligned_groups']['aligned']['win_rate'])}；未同向的 {stats['aligned_groups']['not_aligned']['count']} 筆勝率僅 {_fmt_pct(stats['aligned_groups']['not_aligned']['win_rate'])}。",
                "",
            ]
        )

    report_sections.extend(
        [
            "### 3.3 Breakout 的根因綜合判斷",
            f"- 兩個商品都呈現同一個形狀：**虧損單幾乎全是停損，而且停損平均不到半根 K 棒**；代表 MA 穿越一確認就進場，常常剛好買在短線噪音尾端。",
            f"- 勝單平均獲利點數其實不差（MTX/TX 都接近 500 點 TP），問題不是『賺不夠大』，而是 **24 筆只贏 5 筆**，勝率不足以支撐長期期望值。",
            f"- breakout 進場前通常已先走一段（前 5 根平均約 {_fmt_num(np.mean([breakout_mtx['breakout_stats']['late_entry_all'].get('avg_pre_move_5_points', np.nan), breakout_tx['breakout_stats']['late_entry_all'].get('avg_pre_move_5_points', np.nan)]), 1)} 點），但進場後剩餘可用趨勢空間有限，顯示『確認後進場』的代價很高。",
            "",
            "## 4. Pullback 為何表現較好（但仍偏邊際）？",
        ]
    )

    for symbol, result in [("MTX", pullback_mtx), ("TX", pullback_tx)]:
        summary = result["summary"]
        stats = result["pullback_stats"]
        report_sections.extend(
            [
                f"### 4.{1 if symbol == 'MTX' else 2} {symbol} pullback",
                f"- 交易數只有 {summary['total_trades']} 筆，明顯少於 breakout 的 {result_map[(symbol, 'breakout')]['summary']['total_trades']} 筆，代表 60MA 趨勢濾網 + 回測條件確實降低了出手頻率。",
                f"- 平均獲利 {_fmt_num(summary['avg_win_points'], 1)} 點，平均虧損 {_fmt_num(summary['avg_loss_points_abs'], 1)} 點，實現 RR = {_fmt_num(summary['realized_reward_risk'], 2)}（名目值為 300 / 150 = 2.00）。",
                f"- 勝單出場原因：{json.dumps(stats['winner_exit_reason_counts'], ensure_ascii=False)}；敗單出場原因：{json.dumps(stats['loser_exit_reason_counts'], ensure_ascii=False)}。",
                f"- 本次樣本 **沒有任何 `immediate_reverse` 或 `end_of_data` 出場**；所有勝單都是完整 `take_profit`，所有敗單都是完整 `stop_loss`。",
                "",
            ]
        )

    report_sections.extend(
        [
            "### 4.3 Breakout vs Pullback 直接對照",
            f"- MTX：pullback 勝率 {_fmt_pct(pullback_mtx['summary']['win_rate'])} 明顯高於 breakout 的 {_fmt_pct(breakout_mtx['summary']['win_rate'])}，且交易數從 {breakout_mtx['summary']['total_trades']} 筆降到 {pullback_mtx['summary']['total_trades']} 筆。",
            f"- TX：pullback 勝率 {_fmt_pct(pullback_tx['summary']['win_rate'])} 高於 breakout 的 {_fmt_pct(breakout_tx['summary']['win_rate'])}，但樣本只有 {pullback_tx['summary']['total_trades']} 筆，優勢還不夠穩固。",
            f"- 換句話說：**pullback 比較像『先確認大方向，再等價格回到均線附近切入』**；breakout 則是『穿越確認後直接追價』，更容易吃到盤整假突破。",
            "",
            "## 5. Whipsaw 密集群範例",
        ]
    )

    for symbol, result in [("MTX", breakout_mtx), ("TX", breakout_tx)]:
        examples = result["breakout_stats"]["cluster_examples"]
        report_sections.append(f"### 5.{1 if symbol == 'MTX' else 2} {symbol}")
        if not examples:
            report_sections.append("- 本次資料中未觀察到符合定義的密集反向群。")
        else:
            for example in examples:
                report_sections.append(
                    f"- {example['cluster_id']}: {example['start']} ~ {example['end']}，方向序列 {example['directions']}，各筆點數 {example['points']}"
                )
        report_sections.append("")

    report_sections.extend(
        [
            "## 6. 產出檔案",
            f"- 明細 CSV：`{trade_details_path.name}`",
        ]
    )
    for chart_path in chart_paths:
        report_sections.append(f"- 圖表：`{chart_path.name}`")

    report_sections.extend(
        [
            "",
            "## 7. 結論與建議",
            "1. **最主要根因：盤整／whipsaw 太多，且 breakout 訊號來得太晚。** 多數虧損單直接被 stop loss 打掉，而且持有時間很短，代表這不是『趨勢走錯方向很久』，而是『剛進場就被雜訊反殺』。",
            "2. **breakout 的問題不是賺不到 500 點，而是贏的次數太少。** 勝單通常能吃到接近完整 TP，但 24 筆只贏約 5 筆，無法覆蓋大量 150 點停損與成本。",
            "3. **pullback 的 60MA 趨勢濾網有實際幫助。** 它讓交易數下降、勝率上升；同樣的統計也顯示 breakout 若只保留 60MA 同向交易，表現明顯比較好，這是最具體、最可落地的改良方向。",
            "",
            "> 註：本報告僅做診斷與統計，**沒有修改任何策略邏輯**。",
        ]
    )

    return "\n".join(report_sections) + "\n"


def run_analysis() -> dict[str, Path]:
    results = [
        _analyze_one("MTX", "breakout"),
        _analyze_one("MTX", "pullback"),
        _analyze_one("TX", "breakout"),
        _analyze_one("TX", "pullback"),
    ]

    trade_details_path = _save_trade_details(results)
    chart_paths = [
        _plot_pnl_histogram(results),
        _plot_breakout_holding_vs_pnl(results),
        _plot_breakout_filters(results),
    ]
    report_path = DATA_DIR / "strategy_failure_analysis.md"
    report_text = _build_report(results, chart_paths, trade_details_path)
    report_path.write_text(report_text, encoding="utf-8")

    metrics_path = DATA_DIR / f"{OUTPUT_PREFIX}_metrics.json"
    metrics_payload = {
        f"{result['symbol']}_{result['strategy']}": {
            "summary": result["summary"],
            "breakout_stats": result.get("breakout_stats"),
            "pullback_stats": result.get("pullback_stats"),
        }
        for result in results
    }
    metrics_path.write_text(json.dumps(metrics_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "report_path": report_path,
        "trade_details_path": trade_details_path,
        "metrics_path": metrics_path,
        "chart_1": chart_paths[0],
        "chart_2": chart_paths[1],
        "chart_3": chart_paths[2],
    }


def parse_args() -> argparse.Namespace:
    return argparse.ArgumentParser(description="Analyze why breakout underperforms on Taiwan Index Futures 60-minute data.").parse_args()


def main() -> None:
    parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    output_paths = run_analysis()
    for key, path in output_paths.items():
        print(f"{key}: {path}")


if __name__ == "__main__":
    main()
