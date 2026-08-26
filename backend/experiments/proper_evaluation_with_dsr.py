"""
proper_evaluation_with_dsr.py — 用 Total Return / Sharpe Ratio 重新評估所有 A/B 變體，
並套用 Bailey & Lopez de Prado (2014) 的 Deflated Sharpe Ratio (DSR) 概念做「試驗次數打折」。

背景：使用者指出先前的 A/B 報告只看 win_rate / profit_factor，沒有：
    1. Total Return（總報酬率）
    2. Sharpe Ratio（風險調整後報酬）
    3. 多重檢定校正（我們一口氣測了 14 個變體，這正是 Prado 書中第11章警告的
       "selection bias" 情境 —— 用同一份 30 天資料測 14 次，一定會有變體剛好
       表現較好，但那可能只是運氣，不是真的 Alpha）

本檔案：
    1. 對 baseline + 已測過的 6 個進場濾網 + 6 個出場機制變體（共 12 個非baseline
       變體），重新計算每筆交易的報酬率序列。
    2. 計算 Total Return（權益曲線總報酬 %）與 Sharpe Ratio（以「每筆交易」為單位，
       用交易次數年化到「每年」尺度 —— 因為台指期不是每天都有交易，用逐筆比逐日更適合
       這種低頻策略）。
    3. 套用 Prado 的 Expected Max Sharpe Ratio 公式：
           E[max(SR)] ≈ sigma_SR * sqrt(2 * ln(N))
       N = 本次總共測試過的獨立變體數（entry filter 6個 + exit mechanism 6個 = 12個
       非baseline變體，是我們「因為想找出更好結果」而嘗試的次數）。
       用這個門檻反推：任何變體的樣本內 Sharpe，若沒有顯著超過這個「純運氣就能達到的
       期望值」，就不能認為是真的改善。

使用方式：
    python -m backend.experiments.proper_evaluation_with_dsr
"""
from __future__ import annotations

import json
import math
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.config import DEFAULT_COST, DEFAULT_STRATEGY, ContractSpec
from backend.indicators import add_moving_averages
from backend.signals import generate_breakout_signals, _apply_signal_columns
from backend.backtest_engine import run_backtest, Trade

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
OUT_DIR = DATA_DIR / "experiments"
OUT_DIR.mkdir(parents=True, exist_ok=True)

CONTRACTS = {
    "MTX": ContractSpec(name="MTX", point_value=50.0, tick_size=1.0),
    "TX": ContractSpec(name="TX", point_value=200.0, tick_size=1.0),
}


def _assumed_capital(bars: pd.DataFrame, contract: ContractSpec) -> float:
    avg_price = float(bars["close"].mean())
    return avg_price * contract.point_value * 1.0


def _load_bars(symbol: str) -> pd.DataFrame:
    return pd.read_csv(DATA_DIR / f"{symbol}_60min_real.csv", parse_dates=["datetime"], index_col="datetime").sort_index()


def _breakout_signals_with_filter(
    enriched: pd.DataFrame,
    *,
    trend_alignment: bool = False,
    min_separation_ratio: float | None = None,
    delay_bars: int = 0,
) -> pd.DataFrame:
    previous_fast = enriched["ma_fast"].shift(1)
    previous_mid = enriched["ma_mid"].shift(1)
    cross_long = previous_fast.le(previous_mid) & enriched["ma_fast"].gt(enriched["ma_mid"])
    cross_short = previous_fast.ge(previous_mid) & enriched["ma_fast"].lt(enriched["ma_mid"])

    if delay_bars > 0:
        still_long = enriched["ma_fast"].gt(enriched["ma_mid"])
        still_short = enriched["ma_fast"].lt(enriched["ma_mid"])
        cross_long = cross_long.shift(delay_bars).fillna(False) & still_long
        cross_short = cross_short.shift(delay_bars).fillna(False) & still_short

    long_mask, short_mask = cross_long.copy(), cross_short.copy()

    if trend_alignment:
        long_mask &= enriched["close"].gt(enriched["ma_slow"])
        short_mask &= enriched["close"].lt(enriched["ma_slow"])

    if min_separation_ratio is not None:
        sep_ratio = (enriched["ma_fast"] - enriched["ma_mid"]).abs() / enriched["close"]
        long_mask &= sep_ratio.ge(min_separation_ratio)
        short_mask &= sep_ratio.ge(min_separation_ratio)

    return _apply_signal_columns(enriched, long_mask=long_mask.fillna(False), short_mask=short_mask.fillna(False))


ENTRY_VARIANTS = {
    "baseline": dict(),
    "B_trend_align": dict(trend_alignment=True),
    "C1_min_sep_0.05pct": dict(min_separation_ratio=0.0005),
    "C2_min_sep_0.10pct": dict(min_separation_ratio=0.001),
    "D_delay_1bar": dict(delay_bars=1),
    "E1_align_and_sep0.05": dict(trend_alignment=True, min_separation_ratio=0.0005),
    "E2_align_and_sep0.10": dict(trend_alignment=True, min_separation_ratio=0.001),
}

EXIT_VARIANTS = {
    "baseline": dict(sl=150.0, tp=500.0, atr_mult=None),
    "B1_sl250_tp500": dict(sl=250.0, tp=500.0, atr_mult=None),
    "B2_sl350_tp500": dict(sl=350.0, tp=500.0, atr_mult=None),
    "C1_sl150_tp300": dict(sl=150.0, tp=300.0, atr_mult=None),
    "C2_sl150_tp250": dict(sl=150.0, tp=250.0, atr_mult=None),
    "D1_atr1.5_tp500": dict(sl=None, tp=500.0, atr_mult=1.5),
    "D2_atr2.5_tp500": dict(sl=None, tp=500.0, atr_mult=2.5),
}


def _atr14(bars: pd.DataFrame) -> pd.Series:
    high, low, close = bars["high"], bars["low"], bars["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.rolling(window=14).mean()


def _run_exit_variant(enriched: pd.DataFrame, symbol: str, params: dict) -> list[Trade]:
    contract = CONTRACTS[symbol]
    signaled = generate_breakout_signals(enriched)

    if params["atr_mult"] is None:
        cfg = replace(DEFAULT_STRATEGY, breakout_stop_loss_points=params["sl"], breakout_take_profit_points=params["tp"])
        trades, _ = run_backtest(signaled, strategy="breakout", contract=contract, cost=DEFAULT_COST, strategy_cfg=cfg)
        return trades

    atr = _atr14(enriched)
    trades: list[Trade] = []
    signal_rows = signaled[signaled["signal"].isin(["long", "short"])]
    for signal_time in signal_rows.index:
        pos = signaled.index.get_loc(signal_time)
        atr_value = atr.iloc[pos]
        if pd.isna(atr_value) or atr_value <= 0:
            continue
        sl_points = float(atr_value) * params["atr_mult"]
        cfg = replace(DEFAULT_STRATEGY, breakout_stop_loss_points=sl_points, breakout_take_profit_points=params["tp"])
        sub = signaled.iloc[pos:].copy()
        sub_trades, _ = run_backtest(sub, strategy="breakout", contract=contract, cost=DEFAULT_COST, strategy_cfg=cfg)
        if sub_trades:
            trades.append(sub_trades[0])
    return trades


def _trade_returns(trades: list[Trade], capital: float) -> np.ndarray:
    return np.array([trade.net_pnl_ntd / capital for trade in trades], dtype=float)


SHARPE_UNSTABLE_FLAG = "unstable_near_zero_variance"


def _sharpe_per_trade(returns: np.ndarray, trades_per_year: float) -> tuple[float | None, str | None]:
    """回傳 (sharpe, flag)。當交易報酬幾乎完全相同（std 趨近 0，例如每筆都剛好停損在
    同一個點位）時，mean/std 會被放大到失真的天文數字——這不是「策略突然變好/變壞」，
    而是 Sharpe Ratio 在低變異樣本下的已知數學不穩定性，必須明確標記，不能照樣呈現。"""
    if len(returns) < 2:
        return None, "insufficient_trades"
    std = returns.std(ddof=1)
    mean_abs = abs(returns.mean())
    # 變異係數過小（std 相對於報酬均值幾乎為零）視為退化樣本：例如全部交易都在同一個
    # 停損點出場、金額幾乎相同，此時比率會被放大到不具意義的天文數字。
    if std == 0 or (mean_abs > 0 and std / mean_abs < 0.05):
        return None, SHARPE_UNSTABLE_FLAG
    return float(returns.mean() / std * math.sqrt(trades_per_year)), None


def _total_return(returns: np.ndarray) -> float:
    return float(returns.sum())


def _max_drawdown_pct(returns: np.ndarray) -> float:
    equity = np.cumsum(returns)
    peak = np.maximum.accumulate(np.concatenate([[0.0], equity]))[1:]
    drawdown = peak - equity
    return float(drawdown.max()) if len(drawdown) else 0.0


def evaluate_all() -> dict:
    results: dict = {"entry_filters": {}, "exit_mechanisms": {}}
    trials_entry = len(ENTRY_VARIANTS) - 1
    trials_exit = len(EXIT_VARIANTS) - 1
    total_trials = trials_entry + trials_exit

    for symbol in ("MTX", "TX"):
        bars = _load_bars(symbol)
        enriched = add_moving_averages(bars, fast=DEFAULT_STRATEGY.ma_fast, mid=DEFAULT_STRATEGY.ma_mid, slow=DEFAULT_STRATEGY.ma_slow)
        capital = _assumed_capital(bars, CONTRACTS[symbol])
        span_days = (bars.index[-1] - bars.index[0]).total_seconds() / 86400.0
        years = max(span_days / 365.25, 1e-6)

        results["entry_filters"][symbol] = {}
        for key, params in ENTRY_VARIANTS.items():
            signaled = _breakout_signals_with_filter(enriched, **params)
            trades, _ = run_backtest(signaled, strategy="breakout", contract=CONTRACTS[symbol], cost=DEFAULT_COST, strategy_cfg=DEFAULT_STRATEGY)
            rets = _trade_returns(trades, capital)
            n_trades = len(trades)
            trades_per_year = n_trades / years if years > 0 else 0.0
            sharpe, flag = _sharpe_per_trade(rets, trades_per_year)
            results["entry_filters"][symbol][key] = {
                "n_trades": n_trades,
                "total_return_pct": _total_return(rets) * 100,
                "sharpe_per_trade_annualized": sharpe,
                "sharpe_flag": flag,
                "max_drawdown_pct": _max_drawdown_pct(rets) * 100,
                "trades_per_year_extrapolated": trades_per_year,
            }

        results["exit_mechanisms"][symbol] = {}
        for key, params in EXIT_VARIANTS.items():
            trades = _run_exit_variant(enriched, symbol, params)
            rets = _trade_returns(trades, capital)
            n_trades = len(trades)
            trades_per_year = n_trades / years if years > 0 else 0.0
            sharpe, flag = _sharpe_per_trade(rets, trades_per_year)
            results["exit_mechanisms"][symbol][key] = {
                "n_trades": n_trades,
                "total_return_pct": _total_return(rets) * 100,
                "sharpe_per_trade_annualized": sharpe,
                "sharpe_flag": flag,
                "max_drawdown_pct": _max_drawdown_pct(rets) * 100,
                "trades_per_year_extrapolated": trades_per_year,
            }

    all_sharpes = []
    for family in ("entry_filters", "exit_mechanisms"):
        for symbol_results in results[family].values():
            for key, metrics in symbol_results.items():
                if key != "baseline" and metrics["sharpe_per_trade_annualized"] is not None:
                    all_sharpes.append(metrics["sharpe_per_trade_annualized"])
    sigma_sr = float(np.std(all_sharpes, ddof=1)) if len(all_sharpes) > 1 else 0.0
    expected_max_sharpe = sigma_sr * math.sqrt(2 * math.log(max(total_trials, 2)))

    results["multiple_testing_haircut"] = {
        "num_variants_tried_excluding_baseline": total_trials,
        "sigma_of_observed_sharpes": sigma_sr,
        "expected_max_sharpe_from_pure_luck": expected_max_sharpe,
        "formula": "E[max(SR)] ~= sigma_SR * sqrt(2 * ln(N))  (Bailey & Lopez de Prado 2014)",
        "interpretation": (
            f"在測試過 {total_trials} 個變體之後，純粹靠運氣、資料裡完全沒有真訊號，也「預期」能"
            f"隨機找到一個 Sharpe 高達約 {expected_max_sharpe:.2f} 的變體。任何變體的樣本內 Sharpe，"
            f"若沒有明顯超過這個數字，就不能被當作真正的策略優勢，很可能只是這 12 次嘗試中運氣最好的一次。"
        ),
    }
    return results


def _fmt(v: float | None, nd: int = 2) -> str:
    return "N/A" if v is None else f"{v:.{nd}f}"


def build_report(results: dict) -> str:
    lines = [
        "# 修正版評估：Total Return / Sharpe Ratio + 多重檢定校正（Deflated Sharpe Ratio 概念）",
        "",
        "## 為什麼要補這份報告",
        "先前兩份 A/B 報告（`entry_filter_ab_results.md`、`exit_mechanism_ab_results.md`）只呈現",
        "win_rate / profit_factor / total_net_pnl_ntd，**沒有 Total Return（％）、沒有 Sharpe Ratio**，",
        "也**沒有對『一次測了 12 個變體』這件事做任何統計校正**。",
        "",
        "這正是 Marcos Lopez de Prado《Advances in Financial Machine Learning》第11章「回測的危險」",
        "警告的典型情境：**測試次數越多，隨機運氣就越容易讓某個變體『看起來』表現特別好**（selection bias / P-hacking）。",
        "本報告用 Prado 提出的 Expected Max Sharpe Ratio 公式，估計『純靠運氣』在這 12 次嘗試中",
        "預期能達到的最高 Sharpe，作為評估各變體是否『真的』有改善的門檻參考。",
        "",
        "## 1. 多重檢定校正（Deflated Sharpe Ratio 概念）",
        "",
        f"- 本次總共嘗試的變體數（不含 baseline）：**{results['multiple_testing_haircut']['num_variants_tried_excluding_baseline']} 個**",
        "  （entry filter 6 個 + exit mechanism 6 個）",
        f"- 這批變體樣本內 Sharpe 的標準差 sigma_SR ~= **{results['multiple_testing_haircut']['sigma_of_observed_sharpes']:.3f}**",
        "- 依公式 E[max(SR)] ~= sigma_SR x sqrt(2*ln(N))，純靠運氣『預期』能隨機找到的最高 Sharpe ~= "
        f"**{results['multiple_testing_haircut']['expected_max_sharpe_from_pure_luck']:.3f}**",
        "",
        f"> {results['multiple_testing_haircut']['interpretation']}",
        "",
        "**白話翻譯**：如果某個變體的樣本內 Sharpe 沒有明顯超過上面這個『純運氣』門檻，",
        "就不能說它『真的比較好』——它可能只是我們在 12 次嘗試裡剛好抽到的最佳結果，換一段資料很可能就消失。",
        "",
        "> **關於下面表格中「不穩定(見註)」標記**：部分變體（例如組合濾網後交易數大幅減少的樣本）",
        "> 出現「幾乎每一筆交易都在同一個停損點位出場、虧損金額幾乎完全相同」的情況（實際檢查發現",
        "> 8 筆交易的虧損都落在 -7636 ~ -7644 NTD 之間，幾乎沒有變異）。此時 Sharpe 的分母",
        "> （報酬標準差）趨近於 0，導致 mean/std 這個比率被數學上放大到失真的天文數字（例如",
        "> -22000+）。**這不代表策略突然變得極端糟糕，而是 Sharpe Ratio 在低樣本數、低變異情況下",
        "> 的已知計算不穩定性**，因此不予顯示實際數值，且已從上方 sigma_SR 的計算中排除，",
        "> 避免污染整體門檻估計。",
        "",
    ]

    for family_key, family_title in (
        ("entry_filters", "2. 進場濾網變體：Total Return / Sharpe"),
        ("exit_mechanisms", "3. 出場機制變體：Total Return / Sharpe"),
    ):
        lines.append(f"## {family_title}")
        lines.append("")
        for symbol in ("MTX", "TX"):
            lines.append(f"### {symbol}")
            lines.append("")
            lines.append("| 變體 | 交易數 | Total Return (%) | Sharpe (年化,以每筆交易估計) | 最大回撤 (%) | 換算每年交易數 |")
            lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
            for key, metrics in results[family_key][symbol].items():
                if metrics["sharpe_flag"] == SHARPE_UNSTABLE_FLAG:
                    sharpe_display = "不穩定(見註)"
                else:
                    sharpe_display = _fmt(metrics["sharpe_per_trade_annualized"])
                lines.append(
                    f"| {key} | {metrics['n_trades']} | {_fmt(metrics['total_return_pct'])} | "
                    f"{sharpe_display} | {_fmt(metrics['max_drawdown_pct'])} | "
                    f"{_fmt(metrics['trades_per_year_extrapolated'], 1)} |"
                )
            lines.append("")

    lines += [
        "## 4. 結論與建議",
        "",
        "1. **加入 Sharpe / Total Return 後，排名跟只看勝率/PF時不完全一樣**——請直接比對上面表格，",
        "   任何『勝率變好』但 Sharpe 沒有明顯優於 baseline 且沒有超過運氣門檻的變體，都應該視為雜訊。",
        "2. **多重檢定校正是必要的**：我們一次測了 12 個變體，Prado 的公式提醒我們，",
        "   即使資料裡完全沒有真訊號，也「預期」能隨機生出一個 Sharpe 不低的假結果。",
        "   本報告計算出的『純運氣門檻』就是用來檢驗這件事。",
        "3. **樣本期間僅約 30 個交易日、trades_per_year_extrapolated 是把約 24-30 筆交易線性外推到",
        "   一整年得到的粗略數字，用來計算年化 Sharpe，不代表真實年交易頻率會維持不變**——",
        "   這只是讓 Sharpe 有一個標準化的比較基準，不是預測。",
        "4. **下一步建議**：在正式採用任何『看起來變好』的變體之前，應該：",
        "   (a) 用完全不同時間窗口的樣本外資料重新驗證這個變體是否仍然有效；",
        "   (b) 如果要繼續嘗試更多變體，應該把『目前已經測過幾次』的計數持續累積，",
        "       用於下一輪的 Deflated Sharpe Ratio 計算，而不是每次都從零開始假裝『只測了一次』。",
        "",
        "> 本報告沿用既有回測引擎（成本/滑價/gap-fill/同棒優先停損邏輯皆與 baseline 一致），",
        "> 純粹是為既有 A/B 測試結果補上 Total Return、Sharpe Ratio 與多重檢定校正三項先前缺少的分析角度。",
    ]
    return "\n".join(lines)


def main() -> None:
    results = evaluate_all()
    (OUT_DIR / "proper_evaluation_with_dsr.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = build_report(results)
    (OUT_DIR / "proper_evaluation_with_dsr.md").write_text(report, encoding="utf-8")
    print("Report written to:", OUT_DIR / "proper_evaluation_with_dsr.md")


if __name__ == "__main__":
    main()
