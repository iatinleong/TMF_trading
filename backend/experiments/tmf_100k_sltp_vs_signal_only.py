"""
tmf_100k_sltp_vs_signal_only.py — TMF(微台指) 10萬本金：有停損停利點數 vs 拿掉點數只靠
訊號進出場，兩種出場機制的資金曲線分析報告。

跟 tmf_100k_capital_analysis.md 使用同一套分析方式（100k起始本金、逐筆累加資金曲線、
損益兩平所需勝率、同根K棒巴掉比例、方向拆解、保證金緩衝檢查、合併系統），但比較的維度
從「突破 vs 回測」改成「有點數(with_sltp) vs 無點數(signal_only)」，套用在
突破/跌破、回測/回彈兩套策略上，資料來源與參考報告相同：TMF 60分鐘真實資料。

沿用 backend/experiments/sltp_vs_signal_only_ab_test.py 已驗證過的兩種出場邏輯：
    with_sltp     既有引擎預設（breakout: SL150/TP500；pullback: SL150/TP300 +
                  immediate_reverse）。
    signal_only   拿掉 SL/TP，持倉到下一個反向訊號才於次根開盤出場並反手。

依專案慣例：全資料集回測，不做 walk-forward / train-test 切分。

使用方式：
    python -m backend.experiments.tmf_100k_sltp_vs_signal_only
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Literal

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.config import ContractSpec, DEFAULT_COST, DEFAULT_STRATEGY, COMMISSION_PER_SIDE_BY_CONTRACT
from backend.indicators import add_moving_averages
from backend.signals import generate_breakout_signals, generate_pullback_signals
from backend.backtest_engine import Trade
from backend.experiments.sltp_vs_signal_only_ab_test import _run_with_sltp, _run_signal_only

Strategy = Literal["breakout", "pullback"]
Variant = Literal["with_sltp", "signal_only"]

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = DATA_DIR / "experiments"
OUTPUT_MD = OUTPUT_DIR / "tmf_100k_sltp_vs_signal_only.md"
OUTPUT_JSON = OUTPUT_DIR / "tmf_100k_sltp_vs_signal_only.json"

SYMBOL = "TMF"
CONTRACT = ContractSpec(name="TMF", point_value=10.0, tick_size=1.0)

STARTING_CAPITAL = 100_000.0
INITIAL_MARGIN = 31_800.0   # 期交所 2026/06/18 公告，非群益官方報價，沿用前份報告數字
MAINT_MARGIN = 24_400.0

STRATEGY_LABEL = {"breakout": "突破/跌破 (Breakout)", "pullback": "回測/回彈 (Pullback)"}
VARIANT_LABEL = {"with_sltp": "有點數(SL/TP)", "signal_only": "無點數(純訊號反手)"}
SLTP_DESCRIPTION = {
    "breakout": "SL=150點 / TP=500點",
    "pullback": "SL=150點 / TP=300點 + immediate_reverse",
}
BREAKEVEN_WIN_RATE = {
    "breakout": 150.0 / (150.0 + 500.0),
    "pullback": 150.0 / (150.0 + 300.0),
}


def _load_bars() -> pd.DataFrame:
    frame = pd.read_csv(DATA_DIR / f"{SYMBOL}_60min_real.csv", parse_dates=["datetime"], index_col="datetime")
    frame.index = pd.to_datetime(frame.index)
    return frame.sort_index()


def _prepare_enriched() -> pd.DataFrame:
    bars = _load_bars()
    enriched = add_moving_averages(
        bars, fast=DEFAULT_STRATEGY.ma_fast, mid=DEFAULT_STRATEGY.ma_mid, slow=DEFAULT_STRATEGY.ma_slow,
    )
    enriched.index = pd.to_datetime(enriched.index)
    return enriched.sort_index()


def _signaled_frame(strategy: Strategy, enriched: pd.DataFrame) -> pd.DataFrame:
    if strategy == "breakout":
        return generate_breakout_signals(enriched)
    return generate_pullback_signals(enriched)


def _capital_curve(trades: list[Trade], starting_capital: float) -> tuple[list[dict[str, Any]], float, float, bool]:
    """依出場時間排序，逐筆把淨損益累加進資金，回傳 (curve, ending_capital, max_drawdown_pct, breached_maint_margin)."""
    ordered = sorted(trades, key=lambda t: t.exit_time)
    capital = starting_capital
    peak = starting_capital
    max_dd_pct = 0.0
    breached = False
    curve: list[dict[str, Any]] = []
    for trade in ordered:
        capital += trade.net_pnl_ntd
        peak = max(peak, capital)
        dd_pct = (peak - capital) / peak * 100.0 if peak > 0 else 0.0
        max_dd_pct = max(max_dd_pct, dd_pct)
        if capital < MAINT_MARGIN:
            breached = True
        curve.append(
            {
                "exit_time": str(trade.exit_time),
                "direction": trade.direction,
                "exit_reason": trade.exit_reason,
                "net_pnl_ntd": float(trade.net_pnl_ntd),
                "capital_ntd": float(capital),
            }
        )
    return curve, capital, max_dd_pct, breached


def _direction_breakdown(trades: list[Trade]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for direction in ("long", "short"):
        subset = [t for t in trades if t.direction == direction]
        n = len(subset)
        wins = sum(1 for t in subset if t.net_pnl_ntd > 0)
        whipsaw = sum(1 for t in subset if t.exit_time == t.entry_time)
        pnl = sum(t.net_pnl_ntd for t in subset)
        out[direction] = {
            "n_trades": n,
            "win_rate": (wins / n * 100.0) if n else 0.0,
            "whipsaw_rate": (whipsaw / n * 100.0) if n else 0.0,
            "net_pnl_ntd": float(pnl),
        }
    return out


def _avg_holding_hours(trades: list[Trade]) -> float:
    if not trades:
        return 0.0
    durations = [(t.exit_time - t.entry_time).total_seconds() / 3600.0 for t in trades]
    return sum(durations) / len(durations)


def _whipsaw_rate(trades: list[Trade]) -> float:
    if not trades:
        return 0.0
    whipsaw = sum(1 for t in trades if t.exit_time == t.entry_time)
    return whipsaw / len(trades) * 100.0


def run() -> dict[str, Any]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    enriched = _prepare_enriched()
    span_start, span_end = enriched.index[0], enriched.index[-1]

    results: dict[str, dict[str, Any]] = {}
    all_trades_by_variant: dict[Variant, list[Trade]] = {"with_sltp": [], "signal_only": []}

    for strategy in ("breakout", "pullback"):
        signaled = _signaled_frame(strategy, enriched)

        sltp_trades, sltp_summary = _run_with_sltp(signaled, SYMBOL, strategy)
        signal_trades, signal_summary = _run_signal_only(signaled, SYMBOL, strategy)

        for variant, trades in (("with_sltp", sltp_trades), ("signal_only", signal_trades)):
            curve, ending_capital, max_dd_pct, breached = _capital_curve(trades, STARTING_CAPITAL)
            n = len(trades)
            wins = sum(1 for t in trades if t.net_pnl_ntd > 0)
            win_rate = (wins / n * 100.0) if n else 0.0
            net_pnl = ending_capital - STARTING_CAPITAL
            results[f"{strategy}_{variant}"] = {
                "strategy": strategy,
                "variant": variant,
                "n_trades": n,
                "win_rate": win_rate,
                "breakeven_win_rate": (BREAKEVEN_WIN_RATE[strategy] * 100.0) if variant == "with_sltp" else None,
                "net_pnl_ntd": float(net_pnl),
                "ending_capital_ntd": float(ending_capital),
                "total_return_pct": net_pnl / STARTING_CAPITAL * 100.0,
                "max_drawdown_pct": max_dd_pct,
                "breached_maint_margin": breached,
                "whipsaw_rate_pct": _whipsaw_rate(trades),
                "avg_holding_hours": _avg_holding_hours(trades),
                "direction_breakdown": _direction_breakdown(trades),
                "capital_curve": curve,
            }
            all_trades_by_variant[variant].extend(trades)

    combined: dict[str, dict[str, Any]] = {}
    for variant in ("with_sltp", "signal_only"):
        trades = all_trades_by_variant[variant]
        curve, ending_capital, max_dd_pct, breached = _capital_curve(trades, STARTING_CAPITAL)
        n = len(trades)
        wins = sum(1 for t in trades if t.net_pnl_ntd > 0)
        win_rate = (wins / n * 100.0) if n else 0.0
        net_pnl = ending_capital - STARTING_CAPITAL

        # 最大同時持倉口數：逐筆掃描 entry/exit 事件，計算任一時刻同時開倉數。
        events: list[tuple[pd.Timestamp, int]] = []
        for t in trades:
            events.append((t.entry_time, 1))
            events.append((t.exit_time, -1))
        events.sort(key=lambda e: (e[0], e[1]))  # 出場(-1)優先於同時刻進場，避免高估
        concurrent = 0
        max_concurrent = 0
        for _, delta in events:
            concurrent += delta
            max_concurrent = max(max_concurrent, concurrent)

        combined[variant] = {
            "variant": variant,
            "n_trades": n,
            "win_rate": win_rate,
            "net_pnl_ntd": float(net_pnl),
            "ending_capital_ntd": float(ending_capital),
            "total_return_pct": net_pnl / STARTING_CAPITAL * 100.0,
            "max_drawdown_pct": max_dd_pct,
            "breached_maint_margin": breached,
            "max_concurrent_positions": max_concurrent,
            "required_initial_margin_ntd": max_concurrent * INITIAL_MARGIN,
            "required_maint_margin_ntd": max_concurrent * MAINT_MARGIN,
            "capital_curve": curve,
        }

    return {
        "symbol": SYMBOL,
        "data_span": [str(span_start), str(span_end)],
        "starting_capital": STARTING_CAPITAL,
        "per_strategy_variant": results,
        "combined_by_variant": combined,
    }


def _fmt(value: float, digits: int = 0) -> str:
    return f"{value:,.{digits}f}"


def _fmt_signed(value: float, digits: int = 0) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:,.{digits}f}"


def _fmt_pct(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}%"


def _fmt_pct_signed(value: float, digits: int = 2) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.{digits}f}%"


def write_report(result: dict[str, Any]) -> None:
    per = result["per_strategy_variant"]
    combined = result["combined_by_variant"]
    span_start, span_end = result["data_span"]
    lines: list[str] = []

    lines.append("# TMF(微台指)10萬台幣本金：有點數(SL/TP) vs 無點數(純訊號反手) 分析報告\n")
    lines.append(f"**資料期間**：{span_start} ~ {span_end}（真實逐筆成交資料轉製60分K，來源：期交所`raw_tick`，商品代號`TMF`）")
    lines.append(f"**契約規格**：每點台幣價值 {CONTRACT.point_value:.0f}元／點；原始保證金 {_fmt(INITIAL_MARGIN)}元；維持保證金 {_fmt(MAINT_MARGIN)}元（期交所2026/06/18公告）")
    lines.append(
        f"**成本假設**：單邊手續費 {COMMISSION_PER_SIDE_BY_CONTRACT.get('TMF', DEFAULT_COST.commission_per_side):.0f}元"
        "（TMF無群益官方公告區間，採市場常見報價上緣保守估計，非官方數字）、期交稅每邊0.002%"
    )
    lines.append(f"**本金設定**：起始資金 {_fmt(STARTING_CAPITAL)}元台幣（固定1口）")
    lines.append("**比較維度**：同一套 60K5MA/20MA/60MA 訊號規則下，「維持既有固定 SL/TP」vs「拿掉 SL/TP、只在下一個反向訊號出現時出場反手」\n")
    lines.append("---\n")

    lines.append("## 一、總體績效對比\n")
    lines.append(
        "| 策略 | 版本 | 交易數 | 勝率 | 損益兩平所需勝率 | 淨損益(元) | 結束資金 | **總報酬率** | 最大回撤 | 曾跌破維持保證金? |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for strategy in ("breakout", "pullback"):
        for variant in ("with_sltp", "signal_only"):
            r = per[f"{strategy}_{variant}"]
            breakeven = _fmt_pct(r["breakeven_win_rate"]) + f"（{SLTP_DESCRIPTION[strategy]}）" if r["breakeven_win_rate"] is not None else "無固定SL/TP，不適用"
            lines.append(
                f"| {STRATEGY_LABEL[strategy]} | {VARIANT_LABEL[variant]} | {r['n_trades']} | {_fmt_pct(r['win_rate'])} | "
                f"{breakeven} | {_fmt_signed(r['net_pnl_ntd'])} | {_fmt(r['ending_capital_ntd'])} | "
                f"**{_fmt_pct_signed(r['total_return_pct'])}** | {_fmt_pct(r['max_drawdown_pct'])} | "
                f"{'是' if r['breached_maint_margin'] else '否'} |"
            )
    lines.append("")

    for strategy in ("breakout", "pullback"):
        lines.append(f"## {'二' if strategy == 'breakout' else '三'}、{STRATEGY_LABEL[strategy]} —— 有點數 vs 無點數 深入對比\n")
        for variant in ("with_sltp", "signal_only"):
            r = per[f"{strategy}_{variant}"]
            lines.append(f"### {VARIANT_LABEL[variant]}")
            lines.append(f"- 交易數 {r['n_trades']}，勝率 {_fmt_pct(r['win_rate'])}，平均持倉 {r['avg_holding_hours']:.2f} 小時，同根K棒巴掉比例 {_fmt_pct(r['whipsaw_rate_pct'])}")
            lines.append("\n| 方向 | 交易數 | 勝率 | 同根K棒巴掉 | 淨損益(元) |")
            lines.append("|---|---|---|---|---|")
            for direction, label in (("long", "多方"), ("short", "空方")):
                d = r["direction_breakdown"][direction]
                lines.append(
                    f"| {label} | {d['n_trades']} | {_fmt_pct(d['win_rate'])} | {_fmt_pct(d['whipsaw_rate'])} | {_fmt_signed(d['net_pnl_ntd'])} |"
                )
            lines.append("")
        lines.append("")

    lines.append("## 四、四個策略獨立總結（各自獨立100k本金起算，不與其他策略合併）\n")
    for strategy in ("breakout", "pullback"):
        for variant in ("with_sltp", "signal_only"):
            r = per[f"{strategy}_{variant}"]
            buffer_ratio = STARTING_CAPITAL / MAINT_MARGIN
            lines.append(
                f"- **{STRATEGY_LABEL[strategy]}．{VARIANT_LABEL[variant]}**："
                f"{r['n_trades']}筆交易，勝率{_fmt_pct(r['win_rate'])}，總報酬率{_fmt_pct_signed(r['total_return_pct'])}，"
                f"最大回撤{_fmt_pct(r['max_drawdown_pct'])}，結束資金{_fmt(r['ending_capital_ntd'])}元"
                f"（固定1口，10萬本金對維持保證金緩衝約{buffer_ratio:.2f}倍，"
                f"{'資金曾跌破維持保證金，有強制平倉風險' if r['breached_maint_margin'] else '資金全程未跌破維持保證金'}）。"
            )
    lines.append("")

    lines.append("## 五、注意事項與限制\n")
    lines.append("1. **樣本量小**：僅TMF單一商品、約6週資料，各版本交易數多在10~30筆之間，結論僅代表此特定期間表現，不能外推為長期穩定績效。")
    lines.append("2. **手續費估計**：TMF手續費為市場保守估計，非群益官方公告數字。")
    lines.append("3. **累計多重檢定風險**：本專案至今已測試超過130種變體/參數組合，任何「表現最好」的結果都需要更多樣本驗證才能視為真正的Alpha，而非統計雜訊。")
    lines.append("4. **同根K棒巴掉定義**：進場與出場發生在同一根K棒（`exit_time == entry_time`），代表訊號當根K棒內立即被停損/反手出場。")
    lines.append("5. **本報告以4個策略獨立檢視**：突破/跌破（有點數、無點數）、回測/回彈（有點數、無點數）各自從獨立的10萬本金起算，未假設共用同一帳戶資金或保證金，方便單獨評估每個策略/版本的績效與風險特性。")

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
