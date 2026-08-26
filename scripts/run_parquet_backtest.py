import os
import sys
import json
import logging
import time
from pathlib import Path
from datetime import datetime
from typing import Any, Literal

import pandas as pd

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import ContractSpec, CostConfig, DEFAULT_COST, DEFAULT_STRATEGY, COMMISSION_PER_SIDE_BY_CONTRACT
from backend.indicators import resample_to_60min, add_moving_averages
from backend.signals import generate_breakout_signals, generate_pullback_signals
from backend.backtest_engine import run_backtest, Trade, _OpenPosition
from backend.experiments.sltp_vs_signal_only_ab_test import _run_with_sltp, _run_signal_only
from backend.experiments.tmf_100k_sltp_vs_signal_only import _capital_curve, _direction_breakdown, _avg_holding_hours, _whipsaw_rate

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

Strategy = Literal["breakout", "pullback"]
Variant = Literal["with_sltp", "signal_only"]

SYMBOL = "TMF"
CONTRACT = ContractSpec(name="TMF", point_value=10.0, tick_size=1.0)
STARTING_CAPITAL = 100_000.0
INITIAL_MARGIN = 31_800.0   # 期交所保證金
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

def load_and_resample_parquet_ticks() -> pd.DataFrame:
    parquet_dir = Path(r"C:\Users\user\Desktop\專案\台股量化\data\ticks\TMFR1")
    files = sorted(parquet_dir.glob("*.parquet"))
    logger.info(f"找到 {len(files)} 個 Parquet 檔案。開始轉製 1 分鐘 K 線...")
    
    bars_1m_list = []
    for i, file in enumerate(files):
        try:
            df = pd.read_parquet(file)
            if df.empty:
                continue
            df['ts'] = pd.to_datetime(df['ts'])
            df = df.set_index('ts')
            # Resample
            bars_1m = df['close'].resample('1min').ohlc()
            bars_1m['volume'] = df['volume'].resample('1min').sum().fillna(0).astype(int)
            bars_1m = bars_1m.dropna(subset=['open'])
            bars_1m_list.append(bars_1m)
            if (i + 1) % 20 == 0:
                logger.info(f"已處理 {i + 1}/{len(files)} 個檔案...")
        except Exception as exc:
            logger.error(f"處理檔案 {file.name} 時出錯: {exc}")
            
    concatenated_1m = pd.concat(bars_1m_list)
    concatenated_1m.index.name = 'datetime'
    concatenated_1m = concatenated_1m.sort_index()
    logger.info(f"1 分鐘 K 線轉製完成，共 {len(concatenated_1m)} 筆分鐘級資料。")
    return concatenated_1m

def _signaled_frame(strategy: Strategy, enriched: pd.DataFrame) -> pd.DataFrame:
    if strategy == "breakout":
        return generate_breakout_signals(enriched)
    return generate_pullback_signals(enriched)

def run_all_backtests(enriched: pd.DataFrame) -> dict[str, Any]:
    span_start, span_end = enriched.index[0], enriched.index[-1]
    results: dict[str, dict[str, Any]] = {}
    
    for strategy in ("breakout", "pullback"):
        signaled = _signaled_frame(strategy, enriched)
        
        # Run variants
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
            
    return {
        "symbol": SYMBOL,
        "data_span": [str(span_start), str(span_end)],
        "starting_capital": STARTING_CAPITAL,
        "per_strategy_variant": results,
    }

def generate_backtest_report(result: dict[str, Any], output_path: Path) -> None:
    per = result["per_strategy_variant"]
    span_start, span_end = result["data_span"]
    
    lines = []
    lines.append("# TMF(微台指) 十萬本金 Parquet 歷史 Tick 資料回測分析報告\n")
    lines.append(f"**回測資料期間**：{span_start} ~ {span_end}")
    lines.append(f"**資料來源**：`C:\\Users\\user\\Desktop\\專案\\台股量化\\data\\ticks\\TMFR1` (共 {len(per)} 個策略與變體)")
    lines.append(f"**契約規格**：TMF (微台指)，每點點值 {CONTRACT.point_value} TWD；單邊手續費 {COMMISSION_PER_SIDE_BY_CONTRACT['TMF']} TWD；期交稅 0.002% (萬分之0.2)")
    lines.append(f"**本金與風控**：起始資金 {STARTING_CAPITAL:,.0f} TWD (固定 1 口)；原始保證金 {INITIAL_MARGIN:,.0f} TWD；維持保證金 {MAINT_MARGIN:,.0f} TWD\n")
    lines.append("---\n")
    
    lines.append("## 一、四大策略績效比較總表\n")
    lines.append("| 策略與出場機制 | 交易數 | 勝率 | 總淨損益 (TWD) | 結束資金 (TWD) | **總報酬率** | 最大回撤 | 曾跌破維持保證金？ |")
    lines.append("|---|---|---|---|---|---|---|---|")
    
    for strategy in ("breakout", "pullback"):
        for variant in ("with_sltp", "signal_only"):
            r = per[f"{strategy}_{variant}"]
            breached_str = "**是** (有斷頭風險)" if r["breached_maint_margin"] else "否 (安全)"
            lines.append(
                f"| {STRATEGY_LABEL[strategy]}．{VARIANT_LABEL[variant]} | {r['n_trades']} | {r['win_rate']:.2f}% | "
                f"{r['net_pnl_ntd']:+,.0f} | {r['ending_capital_ntd']:,.0f} | "
                f"**{r['total_return_pct']:+.2f}%** | {r['max_drawdown_pct']:.2f}% | {breached_str} |"
            )
            
    lines.append("\n---\n")
    
    lines.append("## 二、各策略詳細交易指標對比\n")
    for strategy in ("breakout", "pullback"):
        lines.append(f"### {STRATEGY_LABEL[strategy]}\n")
        lines.append(f"*設定規格：{SLTP_DESCRIPTION[strategy]}*\n")
        
        for variant in ("with_sltp", "signal_only"):
            r = per[f"{strategy}_{variant}"]
            lines.append(f"#### {VARIANT_LABEL[variant]}")
            lines.append(f"- **交易筆數**：{r['n_trades']} 筆")
            lines.append(f"- **勝率**：{r['win_rate']:.2f}%")
            lines.append(f"- **總淨損益**：{r['net_pnl_ntd']:+,.0f} TWD")
            lines.append(f"- **最大資金回撤 (MDD)**：{r['max_drawdown_pct']:.2f}%")
            lines.append(f"- **平均持倉時間**：{r['avg_holding_hours']:.2f} 小時")
            lines.append(f"- **同根 K 棒巴掉比例 (Whipsaw)**：{r['whipsaw_rate_pct']:.2f}%")
            
            lines.append("\n| 方向 | 交易數 | 勝率 | 同根K棒巴掉率 | 淨損益 (TWD) |")
            lines.append("|---|---|---|---|---|")
            for direction, dir_label in (("long", "多方"), ("short", "空方")):
                d = r["direction_breakdown"][direction]
                lines.append(
                    f"| {dir_label} | {d['n_trades']} | {d['win_rate']:.2f}% | "
                    f"{d['whipsaw_rate']:.2f}% | {d['net_pnl_ntd']:+,.0f} |"
                )
            lines.append("\n")
            
    lines.append("---\n")
    lines.append("## 三、結論與分析\n")
    
    # Analyze best strategy
    best_strategy = None
    best_return = -99999.0
    for key, val in per.items():
        if val["total_return_pct"] > best_return:
            best_return = val["total_return_pct"]
            best_strategy = key
            
    strategy_name, variant_name = best_strategy.split("_", 1)
    lines.append(f"1. **表現最優策略**：**{STRATEGY_LABEL[strategy_name]}．{VARIANT_LABEL[variant_name]}**，總報酬率達 **{best_return:+.2f}%**，淨損益為 **{per[best_strategy]['net_pnl_ntd']:+,.0f} TWD**。")
    
    # Compare SL/TP vs Signal Only for Breakout
    bk_sltp = per["breakout_with_sltp"]["net_pnl_ntd"]
    bk_sig = per["breakout_signal_only"]["net_pnl_ntd"]
    better_bk = "無點數(純訊號)" if bk_sig > bk_sltp else "有點數(SL/TP)"
    lines.append(f"2. **突破策略對比**：有點數版本淨損益 {bk_sltp:+,.0f} TWD vs 無點數版本 {bk_sig:+,.0f} TWD。**{better_bk}** 機制表現較佳。")
    
    # Compare SL/TP vs Signal Only for Pullback
    pb_sltp = per["pullback_with_sltp"]["net_pnl_ntd"]
    pb_sig = per["pullback_signal_only"]["net_pnl_ntd"]
    better_pb = "無點數(純訊號)" if pb_sig > pb_sltp else "有點數(SL/TP)"
    lines.append(f"3. **回測策略對比**：有點數版本淨損益 {pb_sltp:+,.0f} TWD vs 無點數版本 {pb_sig:+,.0f} TWD。**{better_pb}** 機制表現較佳。")
    
    lines.append("4. **保證金風險警告**：")
    for key, val in per.items():
        if val["breached_maint_margin"]:
            strat, var = key.split("_", 1)
            lines.append(f"   * ⚠️ **{STRATEGY_LABEL[strat]}．{VARIANT_LABEL[var]}** 在歷史回測期間，資金餘額**曾跌破維持保證金限制 ({MAINT_MARGIN:,.0f} TWD)**，若在實盤操作中會有被期貨商強平(斷頭)的極大風險！")
            
    output_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"回測報告已輸出至: {output_path}")

def main():
    csv_60m_path = Path("data/TMFR1_parquet_60min.csv")
    
    if csv_60m_path.exists():
        logger.info(f"偵測到已快取的 60 分鐘 K 線資料: {csv_60m_path}，直接載入...")
        bars_60m = pd.read_csv(csv_60m_path, parse_dates=["datetime"], index_col="datetime")
    else:
        logger.info("未偵測到 60 分鐘 K 線快取。開始從原始 Parquet Tick 載入並聚合...")
        bars_1m = load_and_resample_parquet_ticks()
        logger.info("開始利用 session-aware 邏輯轉製 60 分鐘 K 線...")
        bars_60m = resample_to_60min(bars_1m)
        csv_60m_path.parent.mkdir(parents=True, exist_ok=True)
        bars_60m.to_csv(csv_60m_path)
        logger.info(f"60 分鐘 K 線轉製完成，共 {len(bars_60m)} 根 K 棒，已儲存至 {csv_60m_path}")
        
    logger.info("計算策略均線指標...")
    enriched = add_moving_averages(
        bars_60m,
        fast=DEFAULT_STRATEGY.ma_fast,
        mid=DEFAULT_STRATEGY.ma_mid,
        slow=DEFAULT_STRATEGY.ma_slow
    )
    
    logger.info("開始執行四大策略回測...")
    result = run_all_backtests(enriched)
    
    output_report_path = Path("data/experiments/tmf_parquet_100k_backtest.md")
    output_report_path.parent.mkdir(parents=True, exist_ok=True)
    
    generate_backtest_report(result, output_report_path)
    
    # Also save raw json
    json_path = Path("data/experiments/tmf_parquet_100k_backtest.json")
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
        
    print("\n================ 四大策略回測完成總表 ================")
    per = result["per_strategy_variant"]
    for key, val in per.items():
        strat, var = key.split("_", 1)
        status = "breached!" if val["breached_maint_margin"] else "ok"
        print(f"策略: {STRATEGY_LABEL[strat]:<20} | 機制: {VARIANT_LABEL[var]:<12} | 交易數: {val['n_trades']:>3} | 勝率: {val['win_rate']:>5.2f}% | 淨損益: {val['net_pnl_ntd']:+>10,.0f} TWD | 報酬率: {val['total_return_pct']:>+7.2f}% | 風險: {status}")
    print("======================================================")

if __name__ == "__main__":
    main()
