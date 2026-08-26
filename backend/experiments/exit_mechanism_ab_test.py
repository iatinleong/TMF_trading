from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

import pandas as pd

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parents[1]))
    from backtest_engine import BacktestEngine, Trade, run_backtest, trades_to_dataframe  # type: ignore
    from config import ContractSpec, DEFAULT_COST, DEFAULT_STRATEGY  # type: ignore
    from indicators import add_moving_averages  # type: ignore
    from signals import generate_breakout_signals  # type: ignore
else:  # pragma: no cover - exercised when imported as part of backend package
    from ..backtest_engine import BacktestEngine, Trade, run_backtest, trades_to_dataframe
    from ..config import ContractSpec, DEFAULT_COST, DEFAULT_STRATEGY
    from ..indicators import add_moving_averages
    from ..signals import generate_breakout_signals


Direction = Literal["long", "short"]

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = DATA_DIR / "experiments"
OUTPUT_MD = OUTPUT_DIR / "exit_mechanism_ab_results.md"
OUTPUT_JSON = OUTPUT_DIR / "exit_mechanism_ab_results.json"

SYMBOLS = ("MTX", "TX")
ATR_WINDOW = 14
FIXED_TAKE_PROFIT_FOR_ATR = 500.0

CONTRACT_SPECS: dict[str, ContractSpec] = {
    "MTX": ContractSpec(name="MTX", point_value=50.0, tick_size=1.0),
    "TX": ContractSpec(name="TX", point_value=200.0, tick_size=1.0),
}


@dataclass(frozen=True)
class VariantSpec:
    key: str
    label: str
    stop_loss_points: float | None = None
    take_profit_points: float | None = None
    atr_multiple: float | None = None
    notes: str = ""


@dataclass
class VariablePosition:
    entry_time: pd.Timestamp
    entry_price: float
    direction: Direction
    stop_loss_price: float
    take_profit_price: float


VARIANTS: list[VariantSpec] = [
    VariantSpec(
        key="baseline_sl150_tp500",
        label="A. Baseline SL150 / TP500",
        stop_loss_points=150.0,
        take_profit_points=500.0,
        notes="控制組，對照既有 breakout 結果。",
    ),
    VariantSpec(
        key="wider_sl250_tp500",
        label="B1. Wider SL250 / TP500",
        stop_loss_points=250.0,
        take_profit_points=500.0,
        notes="放寬固定停損，觀察是否能穿過進場後雜訊。",
    ),
    VariantSpec(
        key="wider_sl350_tp500",
        label="B2. Wider SL350 / TP500",
        stop_loss_points=350.0,
        take_profit_points=500.0,
        notes="更寬的固定停損，測試『給更大呼吸空間』是否值得。",
    ),
    VariantSpec(
        key="smaller_tp300_sl150",
        label="C1. SL150 / TP300",
        stop_loss_points=150.0,
        take_profit_points=300.0,
        notes="降低停利門檻，測試較容易達標是否改善勝率。",
    ),
    VariantSpec(
        key="smaller_tp250_sl150",
        label="C2. SL150 / TP250",
        stop_loss_points=150.0,
        take_profit_points=250.0,
        notes="更平衡的報酬風險比，觀察勝率與 PF 是否改善。",
    ),
    VariantSpec(
        key="atr14_sl1_5x_tp500",
        label="D1. ATR14 × 1.5 停損 / TP500",
        atr_multiple=1.5,
        take_profit_points=FIXED_TAKE_PROFIT_FOR_ATR,
        notes="使用 14-bar true range SMA ATR，自適應停損；停利固定 500 點以隔離停損效果。",
    ),
    VariantSpec(
        key="atr14_sl2_5x_tp500",
        label="D2. ATR14 × 2.5 停損 / TP500",
        atr_multiple=2.5,
        take_profit_points=FIXED_TAKE_PROFIT_FOR_ATR,
        notes="較寬 ATR 停損倍數；停利固定 500 點。",
    ),
]


def _load_bars(symbol: str) -> pd.DataFrame:
    frame = pd.read_csv(DATA_DIR / f"{symbol}_60min_real.csv", parse_dates=["datetime"], index_col="datetime")
    frame.index = pd.to_datetime(frame.index)
    return frame.sort_index()


def _compute_atr(df: pd.DataFrame, window: int) -> pd.Series:
    prev_close = df["close"].shift(1)
    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(window=window).mean()


def _prepare_breakout_frame(symbol: str) -> pd.DataFrame:
    bars = _load_bars(symbol)
    enriched = add_moving_averages(
        bars,
        fast=DEFAULT_STRATEGY.ma_fast,
        mid=DEFAULT_STRATEGY.ma_mid,
        slow=DEFAULT_STRATEGY.ma_slow,
    )
    enriched["atr_14"] = _compute_atr(enriched, ATR_WINDOW)
    signaled = generate_breakout_signals(enriched)
    signaled.index = pd.to_datetime(signaled.index)
    return signaled.sort_index()


def _safe_float(value: float | int | None) -> float | None:
    if value is None:
        return None
    value = float(value)
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def _fmt_num(value: float | int | None, digits: int = 1) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):,.{digits}f}"


def _fmt_pct(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "N/A"
    return f"{value * 100:.{digits}f}%"


def _profit_factor_from_summary(summary: dict) -> float | None:
    return _safe_float(summary.get("profit_factor"))


def _summarize_trades(trades: list[Trade], summary: dict, spec: VariantSpec, symbol: str) -> dict:
    frame = trades_to_dataframe(trades)
    wins = frame[frame["gross_pnl_points"] > 0] if not frame.empty else frame
    losses = frame[frame["gross_pnl_points"] <= 0] if not frame.empty else frame

    win_rate = float((frame["net_pnl_ntd"] > 0).mean()) if not frame.empty else 0.0
    avg_win_pts = float(wins["gross_pnl_points"].mean()) if not wins.empty else 0.0
    avg_loss_pts = float(losses["gross_pnl_points"].abs().mean()) if not losses.empty else 0.0
    avg_win_ntd = float(wins["net_pnl_ntd"].mean()) if not wins.empty else 0.0
    avg_loss_ntd = float(losses["net_pnl_ntd"].abs().mean()) if not losses.empty else 0.0
    expectancy_pts = (win_rate * avg_win_pts) - ((1.0 - win_rate) * avg_loss_pts)

    return {
        "variant_key": spec.key,
        "variant_label": spec.label,
        "symbol": symbol,
        "total_trades": int(summary["total_trades"]),
        "win_rate": win_rate,
        "total_net_pnl_ntd": float(summary["total_net_pnl_ntd"]),
        "profit_factor": _profit_factor_from_summary(summary),
        "avg_win_pts": avg_win_pts,
        "avg_loss_pts": avg_loss_pts,
        "avg_win_ntd": avg_win_ntd,
        "avg_loss_ntd": avg_loss_ntd,
        "expectancy_per_trade_pts": expectancy_pts,
        "notes": spec.notes,
    }


def _run_fixed_variant(signaled: pd.DataFrame, symbol: str, spec: VariantSpec) -> dict:
    strategy_cfg = replace(
        DEFAULT_STRATEGY,
        breakout_stop_loss_points=float(spec.stop_loss_points),
        breakout_take_profit_points=float(spec.take_profit_points),
    )
    trades, summary = run_backtest(
        signaled,
        strategy="breakout",
        contract=CONTRACT_SPECS[symbol],
        cost=DEFAULT_COST,
        strategy_cfg=strategy_cfg,
        slippage_points=DEFAULT_COST.slippage_points,
    )
    return _summarize_trades(trades, summary, spec, symbol)


def _evaluate_exit_on_bar(engine: BacktestEngine, position: VariablePosition, bar: pd.Series) -> dict | None:
    open_price = float(bar["open"])
    high_price = float(bar["high"])
    low_price = float(bar["low"])

    if position.direction == "long":
        if open_price <= position.stop_loss_price:
            return {"reason": "stop_loss", "exit_price": engine._apply_slippage(open_price, "long", is_entry=False)}
        if open_price >= position.take_profit_price:
            return {"reason": "take_profit", "exit_price": engine._apply_slippage(open_price, "long", is_entry=False)}

        stop_hit = low_price <= position.stop_loss_price
        take_hit = high_price >= position.take_profit_price
        if stop_hit:
            return {
                "reason": "stop_loss",
                "exit_price": engine._apply_slippage(position.stop_loss_price, "long", is_entry=False),
            }
        if take_hit:
            return {
                "reason": "take_profit",
                "exit_price": engine._apply_slippage(position.take_profit_price, "long", is_entry=False),
            }
        return None

    if open_price >= position.stop_loss_price:
        return {"reason": "stop_loss", "exit_price": engine._apply_slippage(open_price, "short", is_entry=False)}
    if open_price <= position.take_profit_price:
        return {"reason": "take_profit", "exit_price": engine._apply_slippage(open_price, "short", is_entry=False)}

    stop_hit = high_price >= position.stop_loss_price
    take_hit = low_price <= position.take_profit_price
    if stop_hit:
        return {
            "reason": "stop_loss",
            "exit_price": engine._apply_slippage(position.stop_loss_price, "short", is_entry=False),
        }
    if take_hit:
        return {
            "reason": "take_profit",
            "exit_price": engine._apply_slippage(position.take_profit_price, "short", is_entry=False),
        }
    return None


def _run_atr_variant(signaled: pd.DataFrame, symbol: str, spec: VariantSpec) -> dict:
    engine = BacktestEngine(
        strategy="breakout",
        contract=CONTRACT_SPECS[symbol],
        cost=DEFAULT_COST,
        strategy_cfg=DEFAULT_STRATEGY,
        slippage_points=DEFAULT_COST.slippage_points,
    )
    trades: list[Trade] = []
    position: VariablePosition | None = None
    prepared = signaled.copy().sort_index()

    for index in range(len(prepared)):
        bar = prepared.iloc[index]
        bar_time = pd.Timestamp(prepared.index[index])

        if position is None and index > 0:
            signal_row = prepared.iloc[index - 1]
            direction = signal_row.get("signal")
            atr_value = signal_row.get("atr_14")
            if direction in {"long", "short"} and pd.notna(atr_value):
                entry_price = engine._apply_slippage(float(bar["open"]), direction, is_entry=True)
                stop_points = float(atr_value) * float(spec.atr_multiple)
                take_profit_points = float(spec.take_profit_points)
                if direction == "long":
                    stop_loss_price = entry_price - stop_points
                    take_profit_price = entry_price + take_profit_points
                else:
                    stop_loss_price = entry_price + stop_points
                    take_profit_price = entry_price - take_profit_points
                position = VariablePosition(
                    entry_time=bar_time,
                    entry_price=float(entry_price),
                    direction=direction,
                    stop_loss_price=float(stop_loss_price),
                    take_profit_price=float(take_profit_price),
                )

        if position is not None:
            exit_event = _evaluate_exit_on_bar(engine, position, bar)
            if exit_event is not None:
                trades.append(engine._close_trade(position, bar_time, exit_event["exit_price"], exit_event["reason"]))
                position = None

    if position is not None:
        last_time = pd.Timestamp(prepared.index[-1])
        last_close = float(prepared.iloc[-1]["close"])
        trades.append(
            engine._close_trade(
                position,
                last_time,
                engine._apply_slippage(last_close, position.direction, is_entry=False),
                "end_of_data",
            )
        )

    summary = engine._build_summary(trades)
    return _summarize_trades(trades, summary, spec, symbol)


def _variant_metadata() -> list[dict]:
    metadata: list[dict] = []
    for spec in VARIANTS:
        metadata.append(
            {
                "variant_key": spec.key,
                "variant_label": spec.label,
                "stop_loss_points": _safe_float(spec.stop_loss_points),
                "take_profit_points": _safe_float(spec.take_profit_points),
                "atr_window": ATR_WINDOW if spec.atr_multiple is not None else None,
                "atr_multiple": _safe_float(spec.atr_multiple),
                "notes": spec.notes,
            }
        )
    return metadata


def _run_all_variants() -> dict:
    symbol_results: dict[str, list[dict]] = {}
    date_ranges: dict[str, dict[str, str]] = {}

    for symbol in SYMBOLS:
        signaled = _prepare_breakout_frame(symbol)
        date_ranges[symbol] = {
            "start": pd.Timestamp(signaled.index.min()).isoformat(),
            "end": pd.Timestamp(signaled.index.max()).isoformat(),
            "bar_count": int(len(signaled)),
        }
        metrics_list: list[dict] = []
        for spec in VARIANTS:
            if spec.atr_multiple is None:
                metrics = _run_fixed_variant(signaled, symbol, spec)
            else:
                metrics = _run_atr_variant(signaled, symbol, spec)
            metrics_list.append(metrics)
        symbol_results[symbol] = metrics_list

    return {
        "metadata": {
            "strategy": "breakout",
            "data_note": "60分鐘真實資料，訊號於收盤確認、下一根開盤進場；不產生任何圖表。",
            "sample_size_caveat": "樣本僅約 551 根 60 分 K（約 30 個交易日），baseline 每商品僅 24 筆交易；本結果僅供方向性探索，不足以作為穩健統計推論。",
            "atr_method": f"ATR 使用 {ATR_WINDOW} bar 經典 true range 的簡單移動平均（SMA）。ATR 變體僅調整停損寬度，停利固定 {FIXED_TAKE_PROFIT_FOR_ATR:.0f} 點。",
            "symbols": list(SYMBOLS),
            "date_ranges": date_ranges,
            "variants": _variant_metadata(),
        },
        "results_by_symbol": symbol_results,
    }


def _find_best_and_worst(metrics_list: list[dict]) -> tuple[dict, dict]:
    ordered = sorted(metrics_list, key=lambda item: (item["expectancy_per_trade_pts"], item["profit_factor"] or -9999.0))
    return ordered[-1], ordered[0]


def _build_markdown_table(metrics_list: list[dict]) -> str:
    lines = [
        "| 變體 | 交易數 | 勝率 | 總淨損益(NTD) | Profit Factor | 平均獲利(點) | 平均虧損(點) | 平均獲利(NTD) | 平均虧損(NTD) | 每筆期望值(點) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in metrics_list:
        lines.append(
            "| "
            + " | ".join(
                [
                    item["variant_label"],
                    str(item["total_trades"]),
                    _fmt_pct(item["win_rate"]),
                    _fmt_num(item["total_net_pnl_ntd"], 0),
                    _fmt_num(item["profit_factor"], 2),
                    _fmt_num(item["avg_win_pts"], 1),
                    _fmt_num(item["avg_loss_pts"], 1),
                    _fmt_num(item["avg_win_ntd"], 0),
                    _fmt_num(item["avg_loss_ntd"], 0),
                    _fmt_num(item["expectancy_per_trade_pts"], 1),
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def _build_recommendations(results: dict) -> str:
    lines = [
        "## 建議與觀察",
        "",
        "### 方法說明",
        f"- 固定點數變體（A/B/C）直接以 `dataclasses.replace(DEFAULT_STRATEGY, ...)` 搭配 `run_backtest()` 重跑，完整沿用既有成本、滑價、下一根開盤成交、同棒先停損與跳空成交規則。",
        f"- ATR 變體（D）採 {ATR_WINDOW} bar 經典 true range SMA ATR，訊號棒收盤時計算 ATR，於下一根開盤進場，停損距離 = ATR × 倍數，停利固定 {FIXED_TAKE_PROFIT_FOR_ATR:.0f} 點；其餘成交/成本邏輯維持與引擎一致。",
        "- 本輪優先完成 A-D；未額外加入 breakeven / trailing bonus 版本，以免在有限樣本上同時引入過多自由度。",
        "",
    ]

    for symbol in SYMBOLS:
        metrics_list = results["results_by_symbol"][symbol]
        best, worst = _find_best_and_worst(metrics_list)
        lines.extend(
            [
                f"### {symbol}",
                f"- **最佳（依每筆期望值排序）**：{best['variant_label']}，勝率 {_fmt_pct(best['win_rate'])}、PF {_fmt_num(best['profit_factor'], 2)}、每筆期望值 {_fmt_num(best['expectancy_per_trade_pts'], 1)} 點、總淨損益 {_fmt_num(best['total_net_pnl_ntd'], 0)} NTD。",
                f"- **最差**：{worst['variant_label']}，勝率 {_fmt_pct(worst['win_rate'])}、PF {_fmt_num(worst['profit_factor'], 2)}、每筆期望值 {_fmt_num(worst['expectancy_per_trade_pts'], 1)} 點、總淨損益 {_fmt_num(worst['total_net_pnl_ntd'], 0)} NTD。",
                "",
            ]
        )

    lines.extend(
        [
            "### 總結判讀",
            "- 若較寬停損（B）提升勝率但每筆期望值仍惡化，代表問題不只是『150 點太緊』，而是 crossover 訊號本身常發生在趨勢尾端。",
            "- 若較小停利（C）明顯提升 PF / 期望值，代表 breakout 在這段資料更像短波段而非大趨勢延伸，較早獲利了結可能較合理。",
            "- 若 ATR 自適應停損（D）優於固定停損，代表不同波動 regime 下需要彈性停損，而非單一固定點數。",
            "",
            "## 樣本數警告",
            "- 這次資料只有約 551 根 60 分 K（約 30 個交易日），baseline 每商品僅 24 筆交易；不同出場機制又會改變持有時間與後續訊號可交易性，因此各變體交易數並不完全相同。",
            "- 結果只能視為**探索性 / 方向性**結論，不能當成穩健統計證據。若要用於真實資金配置，建議先擴充更長期、跨不同波動環境的 out-of-sample 歷史再驗證。",
        ]
    )
    return "\n".join(lines)


def _write_markdown(results: dict) -> None:
    lines = [
        "# Breakout 出場機制 A/B 測試結果",
        "",
        "## 實驗目的",
        "- 針對 breakout（60K 5MA/20MA 穿越，下一根開盤進場）測試不同停損 / 停利機制，確認虧損是否主要來自停損過緊，或是訊號本身太晚。",
        "- 只輸出數字統計，不產生任何圖表、PNG 或影片。",
        "",
        "## 實驗設定",
        f"- 商品：{', '.join(SYMBOLS)}",
        f"- ATR 版本：{results['metadata']['atr_method']}",
        "- 成交規則：沿用既有回測引擎之下一根開盤成交、同棒先停損、跳空以開盤價成交、含雙邊成本與稅。",
        "",
    ]

    for symbol in SYMBOLS:
        metrics_list = results["results_by_symbol"][symbol]
        date_info = results["metadata"]["date_ranges"][symbol]
        lines.extend(
            [
                f"## {symbol}",
                f"- 資料區間：{date_info['start']} ~ {date_info['end']}",
                f"- K 棒數：{date_info['bar_count']}",
                "",
                _build_markdown_table(metrics_list),
                "",
            ]
        )

    lines.append(_build_recommendations(results))
    OUTPUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_json(results: dict) -> None:
    OUTPUT_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results = _run_all_variants()
    _write_markdown(results)
    _write_json(results)

    for symbol in SYMBOLS:
        best, worst = _find_best_and_worst(results["results_by_symbol"][symbol])
        print(
            f"{symbol}: best={best['variant_label']} "
            f"(win_rate={best['win_rate']:.3f}, pf={0.0 if best['profit_factor'] is None else best['profit_factor']:.3f}, "
            f"expectancy_pts={best['expectancy_per_trade_pts']:.2f}); "
            f"worst={worst['variant_label']} "
            f"(win_rate={worst['win_rate']:.3f}, pf={0.0 if worst['profit_factor'] is None else worst['profit_factor']:.3f}, "
            f"expectancy_pts={worst['expectancy_per_trade_pts']:.2f})"
        )
    print(f"Markdown written: {OUTPUT_MD}")
    print(f"JSON written: {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
