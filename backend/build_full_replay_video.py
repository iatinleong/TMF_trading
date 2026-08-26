"""
build_full_replay_video.py — 把整份「60分K突破/回測策略圖」用逐筆資料重播成一部影片

目的：不是只挑一根K棒示範，而是把 MTX/TX_combined_trades_chart.png 那張完整圖表
（近30個交易日、全部K棒、5/20/60MA、進出場標記）真的用逐筆成交「動畫化」出來——
每一根60分鐘K棒，在畫面上都是真正由該60分鐘內的每一筆成交價「長出來」的（開盤➜
盤中最高/最低➜收盤），而不是一次性畫好的靜態K線。

規則（與 backtest_engine.py 完全一致，不因為看得到逐筆就改變訊號時機）：
    - 5MA/20MA/60MA 一律用「已收盤」K棒的收盤價計算；當前這根尚未收盤的K棒，
      畫面上用「半透明的暫定K棒」+「虛線的暫定MA」呈現盤中變化，但不會據此觸發
      任何進場／出場——這正是要呈現給使用者看的重點：暫定值會抖動，收盤那一刻
      才會定案、才會與策略訊號掛勾。
    - 進場點：對應交易的 entry_time 那根K棒，於「該K棒開盤那一刻」標記（因為
      本策略在訊號K棒收盤後，於下一根K棒開盤執行進場）。
    - 出場點：對應交易的 exit_time 那根K棒，於「逐筆價格首次觸及出場價」那一刻
      標記（若整根K棒都沒觸及則於最後一筆標記，對應 end_of_data / 訊號反向出場）。

輸出：data/<symbol>_full_replay.mp4

使用方式：
    python -m backend.build_full_replay_video --symbol MTX --substeps 6 --fps 20
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.font_manager import FontProperties
from matplotlib.patches import Rectangle

try:
    from .config import DEFAULT_STRATEGY, cost_config_for_contract, ContractSpec
    from .indicators import add_moving_averages
    from .signals import generate_breakout_signals, generate_pullback_signals
    from .backtest_engine import run_backtest
    from .load_taifex_tick import RAW_TICK_DIR, _load_and_filter
except ImportError:  # pragma: no cover
    from config import DEFAULT_STRATEGY, cost_config_for_contract, ContractSpec  # type: ignore
    from indicators import add_moving_averages  # type: ignore
    from signals import generate_breakout_signals, generate_pullback_signals  # type: ignore
    from backtest_engine import run_backtest  # type: ignore
    from load_taifex_tick import RAW_TICK_DIR, _load_and_filter  # type: ignore

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CONTRACT_SPECS: dict[str, ContractSpec] = {
    "TX": ContractSpec(name="TX", point_value=200.0, tick_size=1.0),
    "MTX": ContractSpec(name="MTX", point_value=50.0, tick_size=1.0),
    "TMF": ContractSpec(name="TMF", point_value=10.0, tick_size=1.0),
}
_ZH_BOLD = FontProperties(family=["Microsoft JhengHei", "DejaVu Sans"], weight="bold")
_ZH_NORMAL = FontProperties(family=["Microsoft JhengHei", "DejaVu Sans"], weight="normal")

_ENTRY_STYLE = {
    ("breakout", "long"): dict(marker="^", reason="突破：5MA上穿20MA→做多"),
    ("breakout", "short"): dict(marker="v", reason="跌破：5MA下穿20MA→做空"),
    ("pullback", "long"): dict(marker="^", reason="回測：多頭回測20MA→做多"),
    ("pullback", "short"): dict(marker="v", reason="回彈：空頭回彈20MA→做空"),
}
_EXIT_STYLE = {
    "stop_loss": dict(marker="x", color="black", label="停損"),
    "take_profit": dict(marker="o", color="blue", label="停利"),
    "immediate_reverse": dict(marker="D", color="purple", label="訊號反向"),
    "end_of_data": dict(marker="s", color="gray", label="資料結束"),
}

UP_COLOR, DOWN_COLOR = "red", "green"  # 台指慣例：紅漲綠跌


def _load_bars(symbol: str) -> pd.DataFrame:
    return pd.read_csv(DATA_DIR / f"{symbol}_60min_real.csv", parse_dates=["datetime"], index_col="datetime").sort_index()


def _load_all_ticks(symbol: str) -> pd.DataFrame:
    frames = []
    for csv_path in sorted(RAW_TICK_DIR.glob("Daily_*.csv")):
        day_df = _load_and_filter(csv_path, symbol)
        if not day_df.empty:
            frames.append(day_df)
    return pd.concat(frames, ignore_index=True).sort_values("datetime").reset_index(drop=True)


def _collect_trades(enriched: pd.DataFrame, symbol: str):
    index_lookup = {ts: i for i, ts in enumerate(enriched.index)}
    entries, exits = {}, {}  # bar_idx -> list of dicts
    contract = CONTRACT_SPECS[symbol]
    cost = cost_config_for_contract(symbol)
    for strategy in ("breakout", "pullback"):
        signaled = generate_breakout_signals(enriched) if strategy == "breakout" else generate_pullback_signals(enriched)
        trades, _ = run_backtest(signaled, strategy=strategy, contract=contract, cost=cost)
        for trade in trades:
            entry_idx = index_lookup.get(pd.Timestamp(trade.entry_time))
            exit_idx = index_lookup.get(pd.Timestamp(trade.exit_time))
            if entry_idx is None or exit_idx is None:
                continue
            style = _ENTRY_STYLE[(strategy, trade.direction)]
            entries.setdefault(entry_idx, []).append(
                dict(price=trade.entry_price, direction=trade.direction, marker=style["marker"], reason=style["reason"])
            )
            exit_style = _EXIT_STYLE[trade.exit_reason]
            exits.setdefault(exit_idx, []).append(
                dict(price=trade.exit_price, marker=exit_style["marker"], color=exit_style["color"],
                     label=exit_style["label"], reason=trade.exit_reason, direction=trade.direction)
            )
    return entries, exits


def build_video(symbol: str, substeps: int = 6, fps: int = 20, dpi: int = 100, hd: bool = True, max_bars: int | None = None) -> Path:
    bars = _load_bars(symbol)
    enriched = add_moving_averages(bars, fast=DEFAULT_STRATEGY.ma_fast, mid=DEFAULT_STRATEGY.ma_mid, slow=DEFAULT_STRATEGY.ma_slow)
    ticks = _load_all_ticks(symbol)
    n = len(enriched) if max_bars is None else min(max_bars, len(enriched))
    entries, exits = _collect_trades(enriched, symbol)

    idx = enriched.index.to_list()
    o, h, l, c = (enriched[col].to_numpy() for col in ("open", "high", "low", "close"))
    ma_fast_final, ma_mid_final, ma_slow_final = (enriched[col].to_numpy() for col in ("ma_fast", "ma_mid", "ma_slow"))

    y_lo, y_hi = float(np.nanmin(l)) * 0.995, float(np.nanmax(h)) * 1.005

    # HD 樣式：蠟燭放大、文字放大、標記縮小，與 plot_trades.py --hd 靜態圖風格一致
    figsize = (32, 15) if hd else (20, 9)
    dpi = 220 if hd else dpi
    title_fontsize = 26 if hd else 16
    subtitle_fontsize = 15 if hd else 10
    legend_fontsize = 15 if hd else 8
    annotation_fontsize = 11 if hd else 6
    entry_markersize = 90 if hd else 110
    exit_markersize = 70 if hd else 90
    ma_linewidth = 1.8 if hd else 1
    body_w = 0.75 if hd else 0.6
    axis_labelsize = 13 if hd else 10

    fig, ax = plt.subplots(figsize=figsize)
    ax.set_xlim(-1, n + 1)
    ax.set_ylim(y_lo, y_hi)
    ax.set_ylabel("Price", fontproperties=_ZH_NORMAL, fontsize=axis_labelsize)
    ax.tick_params(axis="both", labelsize=axis_labelsize)
    fig.suptitle(f"{symbol} 60分K 突破/跌破+回測/回彈 策略 逐筆重播（真實成交資料）", fontproperties=_ZH_BOLD, fontsize=title_fontsize)

    ma_fast_line, = ax.plot([], [], color="tab:blue", linewidth=ma_linewidth, label="5MA")
    ma_mid_line, = ax.plot([], [], color="tab:orange", linewidth=ma_linewidth, label="20MA")
    ma_slow_line, = ax.plot([], [], color="tab:gray", linewidth=ma_linewidth, label="60MA")
    ma_fast_prov, = ax.plot([], [], color="tab:blue", linewidth=ma_linewidth, linestyle="dotted", alpha=0.6)
    ma_mid_prov, = ax.plot([], [], color="tab:orange", linewidth=ma_linewidth, linestyle="dotted", alpha=0.6)

    legend_done = False
    tmp_dir = DATA_DIR / f"_{symbol}_replay_frames"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    frame_no = 0

    ma_fast_hist, ma_mid_hist, ma_slow_hist = [], [], []

    def _draw_candle(axis, x, o_, h_, l_, c_, alpha):
        color = UP_COLOR if c_ >= o_ else DOWN_COLOR
        axis.add_line(plt.Line2D([x, x], [l_, h_], color=color, linewidth=1.6 if hd else 1, alpha=alpha))
        y0, height = min(o_, c_), max(abs(c_ - o_), 1e-6)
        axis.add_patch(Rectangle((x - body_w / 2, y0), body_w, height, facecolor=color, edgecolor=color, alpha=alpha))

    def _save_frame():
        nonlocal frame_no
        frame_no += 1
        fig.savefig(tmp_dir / f"frame_{frame_no:06d}.png", dpi=dpi)

    for i in range(n):
        bar_end = idx[i]
        bar_start = idx[i - 1] if i > 0 else bar_end - pd.Timedelta(minutes=60)
        bar_ticks = ticks[(ticks["datetime"] > bar_start) & (ticks["datetime"] <= bar_end)]

        # 已收盤的前段K棒維持固定不動；只重畫「當前這一根」的暫定樣貌
        prov_artists = []

        if bar_ticks.empty:
            # 沒有逐筆資料涵蓋（例如資料窗口以外），直接用官方OHLC一次畫完，不逐筆動畫
            _draw_candle(ax, i, o[i], h[i], l[i], c[i], alpha=1.0)
            ma_fast_hist.append(ma_fast_final[i]); ma_mid_hist.append(ma_mid_final[i]); ma_slow_hist.append(ma_slow_final[i])
        else:
            prices = bar_ticks["close"].to_numpy()
            step_idx = np.linspace(0, len(prices) - 1, num=min(substeps, len(prices)), dtype=int)
            step_idx = sorted(set(step_idx.tolist()) | {len(prices) - 1})

            hist_fast = c[max(0, i - 5):i][-5:] if i >= 5 else None
            hist_mid = c[max(0, i - 20):i][-20:] if i >= 20 else None

            for k, si in enumerate(step_idx):
                prov_close = prices[si]
                prov_open = prices[0]
                prov_high = prices[: si + 1].max()
                prov_low = prices[: si + 1].min()

                for artist in prov_artists:
                    artist.remove()
                prov_artists = []
                is_last = si == len(prices) - 1
                alpha = 1.0 if is_last else 0.45
                color = UP_COLOR if prov_close >= prov_open else DOWN_COLOR
                wick = plt.Line2D([i, i], [prov_low, prov_high], color=color, linewidth=1.6 if hd else 1, alpha=alpha)
                ax.add_line(wick); prov_artists.append(wick)
                y0, height = min(prov_open, prov_close), max(abs(prov_close - prov_open), 1e-6)
                rect = Rectangle((i - body_w / 2, y0), body_w, height, facecolor=color, edgecolor=color, alpha=alpha)
                ax.add_patch(rect); prov_artists.append(rect)

                if hist_fast is not None:
                    pf = (hist_fast.sum() + prov_close) / 5
                    ma_fast_prov.set_data(list(range(i - 1, i + 1)), [ma_fast_hist[-1] if ma_fast_hist else np.nan, pf])
                if hist_mid is not None:
                    pm = (hist_mid.sum() + prov_close) / 20
                    ma_mid_prov.set_data(list(range(i - 1, i + 1)), [ma_mid_hist[-1] if ma_mid_hist else np.nan, pm])

                # 進場標記：於此K棒「開盤」那一刻（第一個substep）出現
                if k == 0 and i in entries:
                    for e in entries[i]:
                        col = "red" if e["direction"] == "long" else "green"
                        mk = ax.scatter([i], [e["price"]], marker=e["marker"], color=col, s=entry_markersize, zorder=6)
                        ax.annotate(e["reason"], xy=(i, e["price"]), xytext=(i, e["price"] * (1.004 if e["direction"] == "long" else 0.996)),
                                    fontproperties=_ZH_NORMAL, fontsize=annotation_fontsize, fontweight="bold" if hd else "normal",
                                    color=col, ha="center",
                                    va="bottom" if e["direction"] == "long" else "top", rotation=90)

                # 出場標記：逐筆價格首次觸及出場價時觸發；若都沒觸及則在最後一步觸發
                if i in exits:
                    still = []
                    for e in exits[i]:
                        touched = (prov_low <= e["price"] <= prov_high)
                        if touched or is_last:
                            ax.scatter([i], [e["price"]], marker=e["marker"], color=e["color"], s=exit_markersize, zorder=6)
                        else:
                            still.append(e)
                    exits[i] = still if not is_last else []

                ax.set_title(f"重播進度：第 {i+1}/{n} 根60分K（{bar_end:%Y-%m-%d %H:%M}）｜此K棒逐筆第 {si+1}/{len(prices)} 筆",
                             fontproperties=_ZH_NORMAL, fontsize=subtitle_fontsize, loc="right")
                _save_frame()

            # 這根K棒正式收盤：暫定樣貌轉為永久固定（保留最後一步畫出的實心版本，不刪除）
            ma_fast_hist.append(ma_fast_final[i]); ma_mid_hist.append(ma_mid_final[i]); ma_slow_hist.append(ma_slow_final[i])

        ma_fast_line.set_data(range(len(ma_fast_hist)), ma_fast_hist)
        ma_mid_line.set_data(range(len(ma_mid_hist)), ma_mid_hist)
        ma_slow_line.set_data(range(len(ma_slow_hist)), ma_slow_hist)
        ma_fast_prov.set_data([], [])
        ma_mid_prov.set_data([], [])

        if not legend_done:
            ax.legend(loc="upper left", prop=_ZH_NORMAL, fontsize=legend_fontsize, ncol=1)
            legend_done = True
        if bar_ticks.empty:
            _save_frame()

    plt.close(fig)

    if max_bars is not None:
        # 預覽模式：不編碼影片，直接回傳最後一張暫定畫面供人工檢視樣式
        frames = sorted(tmp_dir.glob("frame_*.png"))
        preview_path = DATA_DIR / f"{symbol}_replay_preview_frame.png"
        if preview_path.exists():
            preview_path.unlink()
        shutil.copy(frames[-1], preview_path)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return preview_path

    out_path = DATA_DIR / f"{symbol}_full_replay.mp4"
    if out_path.exists():
        out_path.unlink()
    cmd = [
        "ffmpeg", "-y", "-framerate", str(fps),
        "-i", str(tmp_dir / "frame_%06d.png"),
        "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return out_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render full tick-by-tick replay video of the combined trades chart.")
    parser.add_argument("--symbol", required=True, choices=("TX", "MTX", "TMF"))
    parser.add_argument("--substeps", type=int, default=6)
    parser.add_argument("--fps", type=int, default=20)
    parser.add_argument("--max-bars", type=int, default=None, help="僅重播前N根K棒並輸出單張預覽圖，不編碼影片")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    out_path = build_video(args.symbol, substeps=args.substeps, fps=args.fps, max_bars=args.max_bars)
    print(f"輸出: {out_path}")


if __name__ == "__main__":
    main()
