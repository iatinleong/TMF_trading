"""
plot_trades.py — 在60分K線圖上標記進場/出場點位與理由（突破+回測兩策略合併於同一張圖）

用途：視覺化驗證回測訊號與出場邏輯是否合理，方便人工複核。

標記說明：
    進場（依「策略＋方向」用不同形狀/顏色區分）：
        紅色 ▲（實心三角）= 突破策略 做多進場
        綠色 ▼（實心三角）= 突破策略 做空進場
        紅色 ★（星形）    = 回測/回彈策略 做多進場
        綠色 ★（星形）    = 回測/回彈策略 做空進場
    出場（依原因用不同符號/顏色區分，兩策略共用同一套圖例）：
        黑色 x        = 停損 (stop_loss)
        藍色 o        = 停利 (take_profit)
        紫色 D（菱形）= 訊號反向、馬上停利並反向掛單 (immediate_reverse，僅回測策略會出現)
        灰色 s（方形）= 資料結束強制平倉 (end_of_data)
    進出場之間以虛線連接：綠線＝該筆交易獲利，紅線＝該筆交易虧損
        （突破策略連線用點線 dotted，回測策略連線用虛線 dashed，方便區分是哪個策略的交易）
    圖上疊加 5MA(藍)/20MA(橘)/60MA(灰) 供人工核對訊號成因

中文字型注意事項：
    matplotlib 預設字型不含中文字形，若只透過 rcParams 設定，mplfinance 內部產生的
    title 有時仍會退回無中文的字體而顯示缺字方框（tofu）。因此本檔案對「所有」會出現
    中文的文字物件（標題、圖例、進場理由標註）一律明確傳入 FontProperties 指定
    "Microsoft JhengHei"，不依賴 rcParams 的隱性套用，避免同樣的缺字問題重演。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import mplfinance as mpf
import numpy as np
import pandas as pd
from matplotlib.font_manager import FontProperties
from matplotlib.lines import Line2D

try:
    from .config import DEFAULT_STRATEGY, cost_config_for_contract, ContractSpec
    from .indicators import add_moving_averages
    from .signals import generate_breakout_signals, generate_pullback_signals
    from .backtest_engine import run_backtest
except ImportError:  # pragma: no cover - script execution fallback
    from config import DEFAULT_STRATEGY, cost_config_for_contract, ContractSpec  # type: ignore
    from indicators import add_moving_averages  # type: ignore
    from signals import generate_breakout_signals, generate_pullback_signals  # type: ignore
    from backtest_engine import run_backtest  # type: ignore

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

CONTRACT_SPECS: dict[str, ContractSpec] = {
    "TX": ContractSpec(name="TX", point_value=200.0, tick_size=1.0),
    "MTX": ContractSpec(name="MTX", point_value=50.0, tick_size=1.0),
    "TMF": ContractSpec(name="TMF", point_value=10.0, tick_size=1.0),
}

# 明確指定中文字型物件，不倚賴 rcParams（title等元件曾實測會忽略 rcParams 而顯示缺字方框）
_ZH_BOLD = FontProperties(family=["Microsoft JhengHei", "DejaVu Sans"], weight="bold")
_ZH_NORMAL = FontProperties(family=["Microsoft JhengHei", "DejaVu Sans"], weight="normal")

_EXIT_STYLE = {
    "stop_loss": dict(marker="x", color="black", label="停損 stop_loss"),
    "take_profit": dict(marker="o", color="blue", label="停利 take_profit"),
    "immediate_reverse": dict(marker="D", color="purple", label="訊號反向 immediate_reverse"),
    "end_of_data": dict(marker="s", color="gray", label="資料結束 end_of_data"),
}

# (strategy, direction) -> (marker, 進場理由文字，前綴策略名稱方便一眼辨識是哪個策略觸發)
_ENTRY_STYLE = {
    ("breakout", "long"): dict(marker="^", reason="突破：5MA上穿20MA→做多"),
    ("breakout", "short"): dict(marker="v", reason="跌破：5MA下穿20MA→做空"),
    ("pullback", "long"): dict(marker="^", reason="回測：多頭回測20MA→做多"),
    ("pullback", "short"): dict(marker="v", reason="回彈：空頭回彈20MA→做空"),
}
_STRATEGY_LINESTYLE = {"breakout": "dotted", "pullback": "dashed"}
_STRATEGY_LABEL = {"breakout": "突破/跌破", "pullback": "回測/回彈"}


def _load_bars(symbol: str) -> pd.DataFrame:
    return pd.read_csv(DATA_DIR / f"{symbol}_60min_real.csv", parse_dates=["datetime"], index_col="datetime")


def _run_strategy(enriched: pd.DataFrame, strategy: str, symbol: str):
    signaled = generate_breakout_signals(enriched) if strategy == "breakout" else generate_pullback_signals(enriched)
    contract = CONTRACT_SPECS[symbol]
    cost = cost_config_for_contract(symbol)
    trades, summary = run_backtest(signaled, strategy=strategy, contract=contract, cost=cost)
    return signaled, trades, summary


def build_combined_chart(symbol: str, hd: bool = False) -> Path:
    bars = _load_bars(symbol)
    enriched = add_moving_averages(
        bars, fast=DEFAULT_STRATEGY.ma_fast, mid=DEFAULT_STRATEGY.ma_mid, slow=DEFAULT_STRATEGY.ma_slow
    )
    index_lookup = {ts: i for i, ts in enumerate(enriched.index)}
    n = len(enriched)

    # HD 模式：放大圖幅/字體/標記，供單張高解析度檢視或影片逐格輸出使用
    figsize = (32, 15) if hd else (20, 9)
    dpi = 220 if hd else 150
    title_fontsize = 26 if hd else 15
    legend_fontsize = 15 if hd else 8
    annotation_fontsize = 11 if hd else 6
    entry_markersize = 220 if hd else 90
    exit_markersize = 170 if hd else 70
    ma_linewidth = 1.8 if hd else 0.8


    all_trades: list[tuple[str, object]] = []
    summaries: dict[str, dict] = {}
    for strategy in ("breakout", "pullback"):
        _, trades, summary = _run_strategy(enriched, strategy, symbol)
        summaries[strategy] = summary
        for trade in trades:
            all_trades.append((strategy, trade))

    entry_series: dict[tuple[str, str], np.ndarray] = {
        key: np.full(n, np.nan) for key in _ENTRY_STYLE
    }
    exit_series: dict[str, np.ndarray] = {reason: np.full(n, np.nan) for reason in _EXIT_STYLE}
    alines: list[list[tuple]] = []
    aline_colors: list[str] = []
    aline_styles: list[str] = []
    annotations: list[dict] = []

    for strategy, trade in all_trades:
        entry_idx = index_lookup.get(pd.Timestamp(trade.entry_time))
        exit_idx = index_lookup.get(pd.Timestamp(trade.exit_time))
        if entry_idx is None or exit_idx is None:
            continue

        entry_series[(strategy, trade.direction)][entry_idx] = trade.entry_price
        exit_series[trade.exit_reason][exit_idx] = trade.exit_price

        alines.append([(enriched.index[entry_idx], trade.entry_price), (enriched.index[exit_idx], trade.exit_price)])
        aline_colors.append("green" if trade.net_pnl_ntd > 0 else "red")
        aline_styles.append(_STRATEGY_LINESTYLE[strategy])

        annotations.append(
            {
                "idx": entry_idx,
                "price": trade.entry_price,
                "direction": trade.direction,
                "reason": _ENTRY_STYLE[(strategy, trade.direction)]["reason"],
            }
        )

    addplots = [
        mpf.make_addplot(enriched["ma_fast"], color="tab:blue", width=ma_linewidth),
        mpf.make_addplot(enriched["ma_mid"], color="tab:orange", width=ma_linewidth),
        mpf.make_addplot(enriched["ma_slow"], color="tab:gray", width=ma_linewidth),
    ]
    for (strategy, direction), series in entry_series.items():
        if np.isfinite(series).any():
            color = "red" if direction == "long" else "green"
            marker = _ENTRY_STYLE[(strategy, direction)]["marker"]
            addplots.append(mpf.make_addplot(series, type="scatter", markersize=entry_markersize, marker=marker, color=color))
    for reason, series in exit_series.items():
        if np.isfinite(series).any():
            style = _EXIT_STYLE[reason]
            addplots.append(mpf.make_addplot(series, type="scatter", markersize=exit_markersize, marker=style["marker"], color=style["color"]))

    aline_dict = dict(alines=alines, colors=aline_colors, linestyle="dashed", linewidths=1.6 if hd else 0.7)

    fig, axes = mpf.plot(
        enriched[["open", "high", "low", "close"]],
        type="candle",
        volume=False,
        style="yahoo",
        addplot=addplots,
        alines=aline_dict,
        returnfig=True,
        figsize=figsize,
    )
    ax = np.atleast_1d(axes).ravel()[0]
    if hd:
        ax.tick_params(axis="both", labelsize=13)

    # 明確用 FontProperties 設定標題，避免 mplfinance 內部 title 產生的粗體字忽略 rcParams 中文字型
    title_text = f"{symbol} 60分K 突破/跌破 + 回測/回彈 進出場標記（近30個交易日真實資料）"
    ax.set_title(title_text, fontproperties=_ZH_BOLD, fontsize=title_fontsize, pad=16 if hd else 12)

    for ann in annotations:
        color = "red" if ann["direction"] == "long" else "green"
        y_offset = 1.004 if ann["direction"] == "long" else 0.996
        va = "bottom" if ann["direction"] == "long" else "top"
        ax.annotate(
            ann["reason"],
            xy=(ann["idx"], ann["price"]),
            xytext=(ann["idx"], ann["price"] * y_offset),
            fontproperties=_ZH_NORMAL,
            fontsize=annotation_fontsize,
            color=color,
            ha="center",
            va=va,
            rotation=90,
            fontweight="bold" if hd else "normal",
        )

    legend_handles = [
        Line2D([0], [0], marker="^", color="w", markerfacecolor="red", markersize=10, label="做多進場（紅▲，文字標註為哪個策略）"),
        Line2D([0], [0], marker="v", color="w", markerfacecolor="green", markersize=10, label="做空進場（綠▼，文字標註為哪個策略）"),
        Line2D([0], [0], marker="x", color="black", markersize=9, label="停損"),
        Line2D([0], [0], marker="o", color="w", markerfacecolor="blue", markersize=9, label="停利"),
        Line2D([0], [0], marker="D", color="w", markerfacecolor="purple", markersize=9, label="訊號反向"),
        Line2D([0], [0], marker="s", color="w", markerfacecolor="gray", markersize=9, label="資料結束"),
        Line2D([0], [0], color="tab:blue", label="5MA"),
        Line2D([0], [0], color="tab:orange", label="20MA"),
        Line2D([0], [0], color="tab:gray", label="60MA"),
        Line2D([0], [0], color="green", linestyle="dashed", label="獲利交易連線"),
        Line2D([0], [0], color="red", linestyle="dashed", label="虧損交易連線"),
    ]
    ax.legend(handles=legend_handles, prop=_ZH_NORMAL, fontsize=legend_fontsize, loc="upper left", ncol=2)

    suffix = "_hd" if hd else ""
    out_path = DATA_DIR / f"{symbol}_combined_trades_chart{suffix}.png"
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)

    print(f"{symbol}: breakout {summaries['breakout']['total_trades']} 筆, "
          f"pullback {summaries['pullback']['total_trades']} 筆 -> {out_path}")
    print("breakout summary:", summaries["breakout"])
    print("pullback summary:", summaries["pullback"])
    return out_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plot combined breakout+pullback entry/exit markers on one chart.")
    parser.add_argument("--symbol", required=True, choices=("TX", "MTX", "TMF"))
    parser.add_argument("--hd", action="store_true", help="輸出高解析度、大字體版本（供影片逐格輸出或單張高清檢視）")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    build_combined_chart(args.symbol, hd=args.hd)


if __name__ == "__main__":
    main()
