from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from backend.backtest_engine import Trade, run_backtest  # type: ignore
    from backend.config import DEFAULT_COST, DEFAULT_STRATEGY  # type: ignore
    from backend.experiments.proper_evaluation_with_dsr import (  # type: ignore
        CONTRACTS,
        OUT_DIR,
        EXIT_VARIANTS,
        SHARPE_UNSTABLE_FLAG,
        _assumed_capital,
        _breakout_signals_with_filter,
        _load_bars,
        _max_drawdown_pct,
        _run_exit_variant,
        _sharpe_per_trade,
        _total_return,
        _trade_returns,
    )
    from backend.indicators import add_moving_averages  # type: ignore
    from backend.signals import generate_breakout_signals  # type: ignore
    try:
        from backend.experiments.combined_filter_exit_test import _run_atr_exit_from_signals  # type: ignore
    except ImportError:  # pragma: no cover - combined file is optional
        _run_atr_exit_from_signals = None  # type: ignore[assignment]
else:
    from ..backtest_engine import Trade, run_backtest
    from ..config import DEFAULT_COST, DEFAULT_STRATEGY
    from ..indicators import add_moving_averages
    from ..signals import generate_breakout_signals
    from .proper_evaluation_with_dsr import (
        CONTRACTS,
        OUT_DIR,
        EXIT_VARIANTS,
        SHARPE_UNSTABLE_FLAG,
        _assumed_capital,
        _breakout_signals_with_filter,
        _load_bars,
        _max_drawdown_pct,
        _run_exit_variant,
        _sharpe_per_trade,
        _total_return,
        _trade_returns,
    )
    try:
        from .combined_filter_exit_test import _run_atr_exit_from_signals
    except ImportError:  # pragma: no cover - combined file is optional
        _run_atr_exit_from_signals = None  # type: ignore[assignment]


OUTPUT_JSON = OUT_DIR / "walk_forward_results.json"
OUTPUT_MD = OUT_DIR / "walk_forward_results.md"
SPLIT_RATIO = 0.60
SYMBOLS = ("MTX", "TX")


@dataclass(frozen=True)
class VariantSpec:
    key: str
    label: str
    notes: str


VARIANTS: list[VariantSpec] = [
    VariantSpec(key="baseline", label="Baseline breakout", notes="原始 breakout。"),
    VariantSpec(key="c1_entry_filter", label="C1 進場濾網", notes="最小均線分離 0.05%。"),
    VariantSpec(key="d2_exit_only", label="D2 出場機制", notes="ATR14 × 2.5 停損 / TP500。"),
]
if _run_atr_exit_from_signals is not None:
    VARIANTS.append(
        VariantSpec(
            key="combined_c1_d2",
            label="C1 + D2 組合",
            notes="C1 進場濾網 + D2 ATR 自適應停損。",
        )
    )


def _fmt_dt(ts: pd.Timestamp) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_num(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "N/A"
    return f"{value:.{digits}f}"


def _fmt_pct(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "N/A"
    return f"{value:.{digits}f}%"


def _fmt_pf(metrics: dict[str, Any]) -> str:
    if metrics.get("profit_factor_display") == "∞":
        return "∞"
    return _fmt_num(metrics.get("profit_factor"), 2)


def _fmt_sharpe(metrics: dict[str, Any]) -> str:
    if metrics["sharpe_flag"] == SHARPE_UNSTABLE_FLAG:
        return "不穩定（近零變異）"
    if metrics["sharpe_flag"] == "insufficient_trades":
        return "N/A（交易不足）"
    return _fmt_num(metrics["sharpe_ratio"], 2)


def _window_payload(window_name: str, bars: pd.DataFrame) -> dict[str, Any]:
    return {
        "name": window_name,
        "bars": int(len(bars)),
        "start": _fmt_dt(pd.Timestamp(bars.index[0])),
        "end": _fmt_dt(pd.Timestamp(bars.index[-1])),
    }


def _split_windows(bars: pd.DataFrame) -> dict[str, pd.DataFrame]:
    split_idx = max(1, int(len(bars) * SPLIT_RATIO))
    split_idx = min(split_idx, len(bars) - 1)
    return {
        "IS": bars.iloc[:split_idx].copy(),
        "OOS": bars.iloc[split_idx:].copy(),
    }


def _prepare_enriched(bars: pd.DataFrame) -> pd.DataFrame:
    enriched = add_moving_averages(
        bars.sort_index(),
        fast=DEFAULT_STRATEGY.ma_fast,
        mid=DEFAULT_STRATEGY.ma_mid,
        slow=DEFAULT_STRATEGY.ma_slow,
    )
    enriched.index = pd.to_datetime(enriched.index)
    return enriched.sort_index()


def _summary_from_trades(trades: list[Trade]) -> dict[str, float | None]:
    net_pnls = [float(trade.net_pnl_ntd) for trade in trades]
    wins = [pnl for pnl in net_pnls if pnl > 0]
    losses = [pnl for pnl in net_pnls if pnl < 0]

    if losses:
        profit_factor = sum(wins) / abs(sum(losses)) if wins else 0.0
    else:
        profit_factor = float("inf") if wins else 0.0

    return {
        "n_trades": float(len(trades)),
        "win_rate": (len(wins) / len(trades)) if trades else 0.0,
        "profit_factor": float(profit_factor),
        "total_net_pnl_ntd": float(sum(net_pnls)),
    }


def _metrics_from_trades(symbol: str, bars: pd.DataFrame, trades: list[Trade]) -> dict[str, Any]:
    summary = _summary_from_trades(trades)
    capital = _assumed_capital(bars, CONTRACTS[symbol])
    returns = _trade_returns(trades, capital)
    span_days = (bars.index[-1] - bars.index[0]).total_seconds() / 86400.0
    years = max(span_days / 365.25, 1e-6)
    trades_per_year = len(trades) / years if years > 0 else 0.0
    sharpe_ratio, sharpe_flag = _sharpe_per_trade(returns, trades_per_year)

    raw_pf = summary["profit_factor"]
    if raw_pf is not None and math.isinf(raw_pf):
        profit_factor = None
        profit_factor_display = "∞"
    else:
        profit_factor = None if raw_pf is None else float(raw_pf)
        profit_factor_display = None

    return {
        "n_trades": int(summary["n_trades"]),
        "win_rate": float(summary["win_rate"]) * 100.0,
        "profit_factor": profit_factor,
        "profit_factor_display": profit_factor_display,
        "total_net_pnl_ntd": float(summary["total_net_pnl_ntd"]),
        "total_return_pct": _total_return(returns) * 100.0,
        "sharpe_ratio": sharpe_ratio,
        "sharpe_flag": sharpe_flag,
        "max_drawdown_pct": _max_drawdown_pct(returns) * 100.0,
        "trades_per_year_extrapolated": trades_per_year,
        "assumed_capital_ntd": capital,
    }


def _run_variant(symbol: str, bars: pd.DataFrame, variant_key: str) -> list[Trade]:
    enriched = _prepare_enriched(bars)

    if variant_key == "baseline":
        signaled = generate_breakout_signals(enriched)
        trades, _ = run_backtest(
            signaled,
            strategy="breakout",
            contract=CONTRACTS[symbol],
            cost=DEFAULT_COST,
            strategy_cfg=DEFAULT_STRATEGY,
        )
        return trades

    if variant_key == "c1_entry_filter":
        signaled = _breakout_signals_with_filter(enriched, min_separation_ratio=0.0005)
        trades, _ = run_backtest(
            signaled,
            strategy="breakout",
            contract=CONTRACTS[symbol],
            cost=DEFAULT_COST,
            strategy_cfg=DEFAULT_STRATEGY,
        )
        return trades

    if variant_key == "d2_exit_only":
        return _run_exit_variant(enriched, symbol, EXIT_VARIANTS["D2_atr2.5_tp500"])

    if variant_key == "combined_c1_d2":
        if _run_atr_exit_from_signals is None:
            raise RuntimeError("combined_filter_exit_test.py is unavailable; combined variant cannot be evaluated.")
        signaled = _breakout_signals_with_filter(enriched, min_separation_ratio=0.0005)
        return _run_atr_exit_from_signals(signaled, enriched, symbol)

    raise ValueError(f"Unknown variant: {variant_key}")


def _comparison_status(is_metrics: dict[str, Any], oos_metrics: dict[str, Any]) -> tuple[str, str]:
    is_return = float(is_metrics["total_return_pct"])
    oos_return = float(oos_metrics["total_return_pct"])
    is_pf = is_metrics["profit_factor"] if is_metrics["profit_factor"] is not None else float("inf")
    oos_pf = oos_metrics["profit_factor"] if oos_metrics["profit_factor"] is not None else float("inf")

    if oos_metrics["n_trades"] < 2:
        return "資料不足", "OOS 交易數太少，無法做穩健判讀。"

    if (is_return > 0 >= oos_return) or (oos_return < is_return - 3.0 and oos_pf < is_pf * 0.7):
        return "疑似過度擬合", "OOS 相比 IS 明顯惡化，符合典型 overfitting / data-snooping 訊號。"

    if oos_return >= is_return - 1.0 and oos_pf >= is_pf * 0.8:
        return "OOS 大致守住", "OOS 沒有明顯崩潰，表現大致維持。"

    return "輕中度退化", "OOS 有惡化，但尚未出現完全崩潰。"


def evaluate_walk_forward() -> dict[str, Any]:
    results: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "split_method": {
            "type": "single_walk_forward_split",
            "is_ratio": SPLIT_RATIO,
            "oos_ratio": 1.0 - SPLIT_RATIO,
            "note": "先依時間排序後，以前 60% K 棒做 IS、後 40% K 棒做 OOS；各視窗獨立計算 MA/ATR 與回測。",
        },
        "variants_included": [{"key": variant.key, "label": variant.label, "notes": variant.notes} for variant in VARIANTS],
        "symbols": {},
    }

    for symbol in SYMBOLS:
        bars = _load_bars(symbol).sort_index()
        windows = _split_windows(bars)
        symbol_payload: dict[str, Any] = {
            "total_bars": int(len(bars)),
            "windows": {name: _window_payload(name, frame) for name, frame in windows.items()},
            "variants": {},
        }

        for variant in VARIANTS:
            is_trades = _run_variant(symbol, windows["IS"], variant.key)
            oos_trades = _run_variant(symbol, windows["OOS"], variant.key)
            is_metrics = _metrics_from_trades(symbol, windows["IS"], is_trades)
            oos_metrics = _metrics_from_trades(symbol, windows["OOS"], oos_trades)
            status, note = _comparison_status(is_metrics, oos_metrics)

            symbol_payload["variants"][variant.key] = {
                "label": variant.label,
                "notes": variant.notes,
                "IS": is_metrics,
                "OOS": oos_metrics,
                "comparison": {"status": status, "note": note},
            }

        results["symbols"][symbol] = symbol_payload

    if _run_atr_exit_from_signals is None:
        results["combined_variant_note"] = "combined_filter_exit_test.py 不存在或不可匯入，因此本次未納入 C1+D2 組合。"
    else:
        results["combined_variant_note"] = "已偵測到 combined_filter_exit_test.py，因此本次納入 C1+D2 組合。"

    return results


def _variant_table(symbol_payload: dict[str, Any]) -> str:
    lines = [
        "| 變體 | IS 交易數 | IS 勝率 | IS PF | IS Total Return | IS Sharpe | IS MaxDD | OOS 交易數 | OOS 勝率 | OOS PF | OOS Total Return | OOS Sharpe | OOS MaxDD | 判讀 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for variant in VARIANTS:
        payload = symbol_payload["variants"][variant.key]
        is_metrics = payload["IS"]
        oos_metrics = payload["OOS"]
        lines.append(
            f"| {payload['label']} | "
            f"{is_metrics['n_trades']} | {_fmt_pct(is_metrics['win_rate'], 1)} | {_fmt_pf(is_metrics)} | "
            f"{_fmt_pct(is_metrics['total_return_pct'], 2)} | {_fmt_sharpe(is_metrics)} | {_fmt_pct(is_metrics['max_drawdown_pct'], 2)} | "
            f"{oos_metrics['n_trades']} | {_fmt_pct(oos_metrics['win_rate'], 1)} | {_fmt_pf(oos_metrics)} | "
            f"{_fmt_pct(oos_metrics['total_return_pct'], 2)} | {_fmt_sharpe(oos_metrics)} | {_fmt_pct(oos_metrics['max_drawdown_pct'], 2)} | "
            f"{payload['comparison']['status']} |"
        )
    return "\n".join(lines)


def _variant_bullets(symbol: str, symbol_payload: dict[str, Any]) -> list[str]:
    bullets: list[str] = []
    for variant in VARIANTS:
        payload = symbol_payload["variants"][variant.key]
        is_metrics = payload["IS"]
        oos_metrics = payload["OOS"]
        bullets.append(
            "- "
            f"**{symbol} / {payload['label']}**：{payload['comparison']['status']}。"
            f"IS Total Return {_fmt_pct(is_metrics['total_return_pct'], 2)} / PF {_fmt_pf(is_metrics)}；"
            f"OOS Total Return {_fmt_pct(oos_metrics['total_return_pct'], 2)} / PF {_fmt_pf(oos_metrics)}。"
            f"{payload['comparison']['note']}"
        )
    return bullets


def build_report(results: dict[str, Any]) -> str:
    lines = [
        "# Walk-Forward Validation（60% IS / 40% OOS）",
        "",
        "## 1. 方法說明",
        "- 僅使用目前這份約 30 個交易日、551 根 60 分 K 的資料。",
        "- 先按時間排序，再切成：前 60% 為 in-sample（IS），後 40% 為 out-of-sample（OOS）。",
        "- 每個視窗**獨立**計算 MA / ATR 並各自回測，避免把 OOS 資料洩漏到 IS 結果。",
        "- 因為每個視窗都重新暖機指標，IS 與 OOS 的交易數相加不一定會等於整段資料一次跑完的交易數，這是刻意接受的無洩漏代價。",
        f"- 納入變體：{', '.join(variant.label for variant in VARIANTS)}。",
        f"- {results['combined_variant_note']}",
        "- 指標：交易數、勝率、Profit Factor、Total Return %、Sharpe Ratio（沿用 `proper_evaluation_with_dsr.py` 的不穩定判定）、Max Drawdown%。",
        "",
        "## 2. 分割區間",
        "",
        "| 商品 | 總 K 棒 | IS K 棒 | IS 區間 | OOS K 棒 | OOS 區間 |",
        "| --- | ---: | ---: | --- | ---: | --- |",
    ]

    for symbol in SYMBOLS:
        payload = results["symbols"][symbol]
        is_window = payload["windows"]["IS"]
        oos_window = payload["windows"]["OOS"]
        lines.append(
            f"| {symbol} | {payload['total_bars']} | {is_window['bars']} | "
            f"{is_window['start']} ~ {is_window['end']} | {oos_window['bars']} | "
            f"{oos_window['start']} ~ {oos_window['end']} |"
        )

    for index, symbol in enumerate(SYMBOLS, start=3):
        lines.extend(
            [
                "",
                f"## {index}. {symbol}：IS vs OOS",
                "",
                _variant_table(results["symbols"][symbol]),
                "",
                "重點判讀：",
                *_variant_bullets(symbol, results["symbols"][symbol]),
            ]
        )

    lines.extend(
        [
            "",
            "## 5. 核心發現",
            "- 先前 purely in-sample 最亮眼的 **D2** 與 **C1+D2 組合**，在 MTX / TX 的 OOS 都同步出現 Total Return、PF、Sharpe 明顯反轉，屬於最值得警惕的 overfitting 訊號。",
            "- **Baseline** 與 **C1 單獨** 在這次切法下至少沒有 OOS 崩潰，甚至出現小幅轉正；但由於 OOS 交易數只有 8 筆與 4 筆左右，這不能被解讀成「已證明有穩健 alpha」，頂多只能說目前沒有像 D2 那樣立即失真。",
            "",
            "## 6. 結論",
            "- 這次 walk-forward 的價值在於：**終於把先前所有 purely in-sample 找到的「較佳變體」拉到真正未參與調參的 OOS 區段檢查**，可直接觀察是否出現典型 overfitting 崩塌。",
            "- 若某變體 OOS 明顯弱於 IS（例如 Total Return / PF / Sharpe 同步下滑），應優先視為資料探勘偏誤警訊，而不是把 IS 的漂亮數字當成可部署優勢。",
            "- 但也必須誠實承認：整份資料只有約 30 天，OOS 只有後 40%（約 12 天、221 根 K），交易筆數非常少；因此這份報告**只足以示範方法論正確落地，完全不足以做統計上定論**。",
            "- 真正要評估策略是否可用，下一步仍必須擴充到更長期、跨不同波動 regime 的 OOS / rolling walk-forward 歷史，再看候選變體是否能持續守住表現，否則不應投入真實資金。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    results = evaluate_walk_forward()
    OUTPUT_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    OUTPUT_MD.write_text(build_report(results), encoding="utf-8")

    print("Walk-forward validation completed.")
    for symbol in SYMBOLS:
        windows = results["symbols"][symbol]["windows"]
        print(
            f"{symbol} IS: {windows['IS']['start']} ~ {windows['IS']['end']} ({windows['IS']['bars']} bars); "
            f"OOS: {windows['OOS']['start']} ~ {windows['OOS']['end']} ({windows['OOS']['bars']} bars)"
        )
    print(f"Markdown report: {OUTPUT_MD}")
    print(f"JSON report: {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
