"""
build_intrabar_video.py — 把單一根60分鐘K棒「逐筆重播」成影片

目的：具體呈現使用者的疑問——「盤中MA真的有穿越，這就是真實世界，難道要無視嗎？」
做法：挑一根K棒，把該60分鐘內的每一筆真實成交依時間序畫成動畫格：
    - 上半：這根K棒目前為止的逐筆走勢（K棒尚未收盤前，用「最新成交價」畫出暫定線圖）
    - 下半：暫定5MA / 暫定20MA 數值即時變化，並標示兩者的「暫定穿越訊號」
    - 收盤那一刻，用垂直虛線標出「官方收盤值」，並顯示是否與盤中暫定訊號一致

輸出：data/<symbol>_intrabar_<bar_end>.mp4（透過 ffmpeg 由逐格 PNG 合成）

使用方式：
    python -m backend.build_intrabar_video --symbol MTX --bar-end "2026-06-03 22:00:00"
    （若不指定 --bar-end，會自動挑一根「盤中訊號翻轉次數最多」的K棒示範）
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.font_manager import FontProperties

try:
    from .load_taifex_tick import RAW_TICK_DIR, _load_and_filter
except ImportError:  # pragma: no cover
    from load_taifex_tick import RAW_TICK_DIR, _load_and_filter  # type: ignore

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_ZH_BOLD = FontProperties(family=["Microsoft JhengHei", "DejaVu Sans"], weight="bold")
_ZH_NORMAL = FontProperties(family=["Microsoft JhengHei", "DejaVu Sans"], weight="normal")

FAST, MID = 5, 20


def _load_all_ticks(symbol: str) -> pd.DataFrame:
    frames = []
    for csv_path in sorted(RAW_TICK_DIR.glob("Daily_*.csv")):
        day_df = _load_and_filter(csv_path, symbol)
        if not day_df.empty:
            frames.append(day_df)
    return pd.concat(frames, ignore_index=True).sort_values("datetime").reset_index(drop=True)


def _pick_demo_bar(symbol: str, bars: pd.DataFrame, ticks: pd.DataFrame) -> pd.Timestamp:
    """自動挑一根「盤中訊號翻轉次數最多」的K棒，最能展示上蹿下跳的效果。"""
    closes = bars["close"].to_list()
    idx = bars.index.to_list()
    best_bar, best_flips = None, -1
    for i in range(MID, len(idx)):
        bar_end, bar_start = idx[i], idx[i - 1]
        hist_fast, hist_mid = closes[i - FAST : i], closes[i - MID : i]
        bar_ticks = ticks[(ticks["datetime"] > bar_start) & (ticks["datetime"] <= bar_end)]
        if len(bar_ticks) < 200:
            continue
        signs = []
        for price in bar_ticks["close"]:
            ma_fast = (sum(hist_fast[1:]) + price) / FAST
            ma_mid = (sum(hist_mid[1:]) + price) / MID
            signs.append(1 if ma_fast > ma_mid else -1)
        flips = sum(1 for a, b in zip(signs, signs[1:]) if a != b)
        if flips > best_flips:
            best_flips, best_bar = flips, bar_end
    return best_bar


def build_video(symbol: str, bar_end: str | None, fps: int = 12, max_frames: int = 240) -> Path:
    bars = pd.read_csv(DATA_DIR / f"{symbol}_60min_real.csv", parse_dates=["datetime"], index_col="datetime").sort_index()
    ticks = _load_all_ticks(symbol)
    idx = bars.index.to_list()
    closes = bars["close"].to_list()

    if bar_end is None:
        bar_end_ts = _pick_demo_bar(symbol, bars, ticks)
        print(f"自動挑選示範K棒: {bar_end_ts}")
    else:
        bar_end_ts = pd.Timestamp(bar_end)

    i = idx.index(bar_end_ts)
    bar_start_ts = idx[i - 1]
    hist_fast = closes[i - FAST : i]
    hist_mid = closes[i - MID : i]
    official_close = closes[i]
    official_ma_fast = (sum(hist_fast[1:]) + official_close) / FAST
    official_ma_mid = (sum(hist_mid[1:]) + official_close) / MID
    official_sign = "多頭 (5MA>20MA)" if official_ma_fast > official_ma_mid else "空頭 (5MA<20MA)"

    bar_ticks = ticks[(ticks["datetime"] > bar_start_ts) & (ticks["datetime"] <= bar_end_ts)].reset_index(drop=True)
    if bar_ticks.empty:
        raise RuntimeError(f"No ticks found for bar window ({bar_start_ts}, {bar_end_ts}]")

    # 逐筆太多（可能上萬筆），抽樣到 max_frames 格，但務必保留最後一筆（=收盤價）
    n_ticks = len(bar_ticks)
    if n_ticks > max_frames:
        sample_idx = sorted(set(list(range(0, n_ticks, max(1, n_ticks // max_frames))) + [n_ticks - 1]))
        bar_ticks = bar_ticks.iloc[sample_idx].reset_index(drop=True)

    prices, times = bar_ticks["close"].to_list(), bar_ticks["datetime"].to_list()
    prov_fast_series, prov_mid_series, prov_signs = [], [], []
    for price in prices:
        pf = (sum(hist_fast[1:]) + price) / FAST
        pm = (sum(hist_mid[1:]) + price) / MID
        prov_fast_series.append(pf)
        prov_mid_series.append(pm)
        prov_signs.append("多頭" if pf > pm else "空頭")

    tmp_dir = Path(tempfile.mkdtemp(prefix=f"{symbol}_intrabar_"))
    n_frames = len(prices)
    for f in range(1, n_frames + 1):
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7), gridspec_kw={"height_ratios": [2, 1]}, sharex=False)

        ax1.plot(range(f), prices[:f], color="black", linewidth=1.2)
        ax1.axhline(official_ma_fast, color="tab:blue", linestyle="dotted", linewidth=0.8, alpha=0.5)
        ax1.axhline(official_ma_mid, color="tab:orange", linestyle="dotted", linewidth=0.8, alpha=0.5)
        cur_sign_color = "red" if prov_signs[f - 1] == "多頭" else "green"
        ax1.scatter([f - 1], [prices[f - 1]], color=cur_sign_color, s=40, zorder=5)
        ax1.set_title(
            f"{symbol} {bar_start_ts:%m/%d %H:%M}→{bar_end_ts:%H:%M} 逐筆重播（第{f}/{n_frames}筆，共{n_ticks}筆真實成交）",
            fontproperties=_ZH_BOLD, fontsize=12,
        )
        ax1.set_ylabel("成交價（暫定K棒走勢）", fontproperties=_ZH_NORMAL)
        ax1.text(
            0.01, 0.02, f"目前時間: {times[f - 1]:%H:%M:%S}   目前暫定訊號: {prov_signs[f-1]}",
            transform=ax1.transAxes, fontproperties=_ZH_NORMAL, fontsize=10, color=cur_sign_color,
        )

        ax2.plot(range(f), prov_fast_series[:f], color="tab:blue", label="暫定5MA")
        ax2.plot(range(f), prov_mid_series[:f], color="tab:orange", label="暫定20MA")
        if f == n_frames:
            ax2.axhline(official_ma_fast, color="tab:blue", linestyle="dashed", linewidth=1.5)
            ax2.axhline(official_ma_mid, color="tab:orange", linestyle="dashed", linewidth=1.5)
            ax2.text(
                0.01, 0.88,
                f"【收盤定案】5MA={official_ma_fast:.1f}  20MA={official_ma_mid:.1f}  → 官方訊號：{official_sign}",
                transform=ax2.transAxes, fontproperties=_ZH_BOLD, fontsize=11, color="darkred",
            )
        ax2.set_ylabel("暫定MA數值", fontproperties=_ZH_NORMAL)
        ax2.set_xlabel("逐筆序號（此K棒內）", fontproperties=_ZH_NORMAL)
        ax2.legend(prop=_ZH_NORMAL, loc="upper right", fontsize=9)

        fig.tight_layout()
        fig.savefig(tmp_dir / f"frame_{f:05d}.png", dpi=110)
        plt.close(fig)

    out_path = DATA_DIR / f"{symbol}_intrabar_{bar_end_ts:%Y%m%d_%H%M}.mp4"
    if out_path.exists():
        out_path.unlink()
    cmd = [
        "ffmpeg", "-y", "-framerate", str(fps),
        "-i", str(tmp_dir / "frame_%05d.png"),
        "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return out_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render a video replaying real tick-by-tick price action within one 60-min bar.")
    parser.add_argument("--symbol", required=True, choices=("TX", "MTX"))
    parser.add_argument("--bar-end", default=None, help='e.g. "2026-06-03 22:00:00"; omit to auto-pick a demo bar')
    parser.add_argument("--fps", type=int, default=12)
    parser.add_argument("--max-frames", type=int, default=240)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    out_path = build_video(args.symbol, args.bar_end, fps=args.fps, max_frames=args.max_frames)
    print(f"影片已產生: {out_path}")


if __name__ == "__main__":
    main()
