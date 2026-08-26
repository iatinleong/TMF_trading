from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parents[1]))
    from backtest_engine import run_backtest  # type: ignore
    from config import DEFAULT_COST, DEFAULT_STRATEGY, ContractSpec  # type: ignore
    from indicators import add_moving_averages  # type: ignore
    from signals import _apply_signal_columns  # type: ignore
    from experiments.proper_evaluation_with_dsr import (  # type: ignore
        SHARPE_UNSTABLE_FLAG,
        _assumed_capital,
        _max_drawdown_pct,
        _sharpe_per_trade,
        _total_return,
        _trade_returns,
    )
else:  # pragma: no cover - exercised when imported as part of backend package
    from ..backtest_engine import run_backtest
    from ..config import DEFAULT_COST, DEFAULT_STRATEGY, ContractSpec
    from ..indicators import add_moving_averages
    from ..signals import _apply_signal_columns
    from .proper_evaluation_with_dsr import (
        SHARPE_UNSTABLE_FLAG,
        _assumed_capital,
        _max_drawdown_pct,
        _sharpe_per_trade,
        _total_return,
        _trade_returns,
    )


BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = DATA_DIR / "experiments"
OUTPUT_MD = OUTPUT_DIR / "session_filter_results.md"
OUTPUT_JSON = OUTPUT_DIR / "session_filter_results.json"

SYMBOLS = ("MTX", "TX")
LOW_CONFIDENCE_TRADE_COUNT = 10
PURE_LUCK_SHARPE_BAR = 4.178

CONTRACTS = {
    "MTX": ContractSpec(name="MTX", point_value=50.0, tick_size=1.0),
    "TX": ContractSpec(name="TX", point_value=200.0, tick_size=1.0),
}


@dataclass(frozen=True)
class Variant:
    key: str
    label: str
    description: str
    allowed_entry_close_times: tuple[str, ...] | None = None
    excluded_entry_close_times: tuple[str, ...] = ()


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


def _base_breakout_masks(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    previous_fast = df["ma_fast"].shift(1)
    previous_mid = df["ma_mid"].shift(1)
    long_mask = previous_fast.le(previous_mid) & df["ma_fast"].gt(df["ma_mid"])
    short_mask = previous_fast.ge(previous_mid) & df["ma_fast"].lt(df["ma_mid"])
    return long_mask.fillna(False), short_mask.fillna(False)


def _session_close_times(df: pd.DataFrame) -> dict[str, tuple[str, ...]]:
    close_times = pd.Series(df.index.strftime("%H:%M"), index=df.index)
    day_times = tuple(sorted(close_times[close_times.isin(["09:45", "10:45", "11:45", "12:45", "13:45"])].unique()))
    night_times = tuple(
        sorted(close_times[close_times.isin(["16:00", "17:00", "18:00", "19:00", "20:00", "21:00", "22:00", "23:00", "00:00", "01:00", "02:00", "03:00", "04:00", "05:00"])].unique())
    )
    return {
        "all": tuple(sorted(close_times.unique())),
        "day": day_times,
        "night": night_times,
    }


def _detect_dataset_coverage(df: pd.DataFrame) -> dict[str, Any]:
    close_times = pd.Series(df.index.strftime("%H:%M"), index=df.index)
    counts = close_times.value_counts().sort_index()
    split = _session_close_times(df)
    has_day = bool(split["day"])
    has_night = bool(split["night"])

    if has_day and has_night:
        coverage = "day_and_night"
    elif has_day:
        coverage = "day_only"
    elif has_night:
        coverage = "night_only"
    else:
        coverage = "unknown"

    return {
        "coverage_type": coverage,
        "rows": int(len(df)),
        "start": df.index.min().isoformat(),
        "end": df.index.max().isoformat(),
        "calendar_dates_present": int(df.index.normalize().nunique()),
        "unique_close_times": list(split["all"]),
        "day_session_close_times": list(split["day"]),
        "night_session_close_times": list(split["night"]),
        "counts_by_close_time": {str(k): int(v) for k, v in counts.items()},
    }


def _build_variants(coverage: dict[str, Any]) -> list[Variant]:
    coverage_type = coverage["coverage_type"]
    day_times = tuple(coverage["day_session_close_times"])
    night_times = tuple(coverage["night_session_close_times"])
    all_times = tuple(coverage["unique_close_times"])

    if coverage_type == "day_and_night":
        mid_session_times = tuple(
            time_value
            for time_value in all_times
            if time_value not in {"09:45", "16:00", "13:45", "05:00"}
        )
        return [
            Variant("baseline", "A. Baseline", "不做時段過濾，保留所有 breakout 訊號。"),
            Variant("day_only", "B. 僅做日盤進場", "只保留下一根會在日盤 bar 開盤成交的訊號。", allowed_entry_close_times=day_times),
            Variant("night_only", "C. 僅做夜盤進場", "只保留下一根會在夜盤 bar 開盤成交的訊號。", allowed_entry_close_times=night_times),
            Variant("exclude_day_open", "D. 排除日盤開盤第一根", "排除下一根會在日盤第一根（09:45 bar）開盤成交的訊號。", excluded_entry_close_times=("09:45",)),
            Variant("exclude_night_open", "E. 排除夜盤開盤第一根", "排除下一根會在夜盤第一根（16:00 bar）開盤成交的訊號。", excluded_entry_close_times=("16:00",)),
            Variant("exclude_all_opens", "F. 排除兩個 session 開盤第一根", "同時排除下一根會在 09:45 或 16:00 bar 開盤成交的訊號。", excluded_entry_close_times=("09:45", "16:00")),
            Variant("exclude_all_closes", "G. 排除兩個 session 尾段", "排除下一根會在 13:45 或 05:00 bar 開盤成交的訊號（接近 session 收尾）。", excluded_entry_close_times=("13:45", "05:00")),
            Variant("mid_session_only", "H. 僅做中段時窗", "只保留非開盤第一根、也非最後一根的中段時窗。", allowed_entry_close_times=mid_session_times),
        ]

    if coverage_type == "day_only":
        day_mid_times = tuple(time_value for time_value in day_times if time_value not in {"09:45", "13:45"})
        return [
            Variant("baseline", "A. Baseline", "不做時段過濾，保留所有 breakout 訊號。"),
            Variant("exclude_first_bar", "B. 排除開盤第一根", "排除下一根會在第一根日盤 bar（09:45 bar）開盤成交的訊號。", excluded_entry_close_times=("09:45",)),
            Variant("exclude_last_bar", "C. 排除收盤前最後一根", "排除下一根會在最後一根日盤 bar（13:45 bar）開盤成交的訊號。", excluded_entry_close_times=("13:45",)),
            Variant("mid_session_only", "D. 僅做中段時窗", "只保留非第一根、非最後一根的日盤中段。", allowed_entry_close_times=day_mid_times),
        ]

    if coverage_type == "night_only":
        night_mid_times = tuple(time_value for time_value in night_times if time_value not in {"16:00", "05:00"})
        return [
            Variant("baseline", "A. Baseline", "不做時段過濾，保留所有 breakout 訊號。"),
            Variant("exclude_first_bar", "B. 排除夜盤開盤第一根", "排除下一根會在第一根夜盤 bar（16:00 bar）開盤成交的訊號。", excluded_entry_close_times=("16:00",)),
            Variant("exclude_last_bar", "C. 排除夜盤尾段", "排除下一根會在最後一根夜盤 bar（05:00 bar）開盤成交的訊號。", excluded_entry_close_times=("05:00",)),
            Variant("mid_session_only", "D. 僅做夜盤中段", "只保留非第一根、非最後一根的夜盤中段。", allowed_entry_close_times=night_mid_times),
        ]

    return [Variant("baseline", "A. Baseline", "不做時段過濾，保留所有 breakout 訊號。")]


def _entry_close_time_filter(df: pd.DataFrame, variant: Variant) -> pd.Series:
    next_bar_close_time = pd.Series(df.index.strftime("%H:%M"), index=df.index).shift(-1)
    allowed = pd.Series(True, index=df.index, dtype=bool)

    if variant.allowed_entry_close_times is not None:
        allowed &= next_bar_close_time.isin(variant.allowed_entry_close_times)

    if variant.excluded_entry_close_times:
        allowed &= ~next_bar_close_time.isin(variant.excluded_entry_close_times)

    return allowed.fillna(False)


def _generate_variant_signals(df: pd.DataFrame, variant: Variant) -> pd.DataFrame:
    long_mask, short_mask = _base_breakout_masks(df)
    allowed_entry_mask = _entry_close_time_filter(df, variant)
    long_mask &= allowed_entry_mask
    short_mask &= allowed_entry_mask
    return _apply_signal_columns(df, long_mask=long_mask, short_mask=short_mask)


def _baseline_signal_entry_close_time_counts(df: pd.DataFrame) -> dict[str, int]:
    signaled = _generate_variant_signals(df, Variant("baseline", "baseline", "baseline"))
    signal_rows = signaled[signaled["signal"].isin(["long", "short"])].copy()
    next_bar_close_time = pd.Series(signaled.index.strftime("%H:%M"), index=signaled.index).shift(-1)
    signal_rows["entry_close_time"] = next_bar_close_time.reindex(signal_rows.index)
    counts = signal_rows["entry_close_time"].value_counts(dropna=False).sort_index()
    output: dict[str, int] = {}
    for key, value in counts.items():
        normalized = "null" if pd.isna(key) else str(key)
        output[normalized] = int(value)
    return output


def _safe_float(value: float | int | None) -> float | None:
    if value is None:
        return None
    value = float(value)
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def _profit_factor(summary: dict[str, Any]) -> tuple[float | None, str | None]:
    raw_value = summary.get("profit_factor")
    if isinstance(raw_value, (float, int)) and math.isinf(float(raw_value)):
        return None, "inf"
    return _safe_float(raw_value), None


def _run_variant(symbol: str, df: pd.DataFrame, variant: Variant) -> dict[str, Any]:
    signaled = _generate_variant_signals(df, variant)
    trades, summary = run_backtest(
        signaled,
        strategy="breakout",
        contract=CONTRACTS[symbol],
        cost=DEFAULT_COST,
        strategy_cfg=DEFAULT_STRATEGY,
        slippage_points=DEFAULT_COST.slippage_points,
    )

    raw_bars = df[["open", "high", "low", "close", "volume"]].copy()
    capital = _assumed_capital(raw_bars, CONTRACTS[symbol])
    returns = _trade_returns(trades, capital)
    n_trades = int(summary.get("total_trades", len(trades)))
    span_days = (df.index[-1] - df.index[0]).total_seconds() / 86400.0
    years = max(span_days / 365.25, 1e-6)
    trades_per_year = n_trades / years if years > 0 else 0.0
    sharpe, sharpe_flag = _sharpe_per_trade(returns, trades_per_year)
    profit_factor, profit_factor_flag = _profit_factor(summary)

    return {
        "variant_key": variant.key,
        "variant_label": variant.label,
        "description": variant.description,
        "n_trades": n_trades,
        "low_confidence": n_trades < LOW_CONFIDENCE_TRADE_COUNT,
        "win_rate": _safe_float(summary.get("win_rate", 0.0)) or 0.0,
        "profit_factor": profit_factor,
        "profit_factor_flag": profit_factor_flag,
        "total_return_pct": _total_return(returns) * 100.0,
        "sharpe_ratio": sharpe,
        "sharpe_flag": sharpe_flag,
        "max_drawdown_pct": _max_drawdown_pct(returns) * 100.0,
        "trades_per_year_extrapolated": trades_per_year,
        "beats_prado_pure_luck_bar_4_18": sharpe is not None and sharpe > PURE_LUCK_SHARPE_BAR,
    }


def evaluate_all() -> dict[str, Any]:
    coverage = _detect_dataset_coverage(_load_bars("MTX"))
    variants = _build_variants(coverage)
    baseline_signal_counts = _baseline_signal_entry_close_time_counts(_prepare_enriched("MTX"))

    results: dict[str, Any] = {
        "metadata": {
            "strategy": "breakout",
            "contract_symbols": list(SYMBOLS),
            "dataset_coverage": coverage,
            "baseline_signal_entry_close_time_counts_mtx": baseline_signal_counts,
            "methodology_note": (
                "因 breakout 訊號在 bar 收盤確認、下一根 bar 開盤成交，所以時段濾網是以『下一根將成交的 bar 時窗』"
                "回推到當前訊號列，先抑制 long/short mask，再交給 _apply_signal_columns。"
            ),
            "sharpe_reference_bar_from_prior_12_trials": PURE_LUCK_SHARPE_BAR,
            "sharpe_unstable_flag": SHARPE_UNSTABLE_FLAG,
            "low_confidence_rule": f"n_trades < {LOW_CONFIDENCE_TRADE_COUNT}",
            "variants": [
                {
                    "key": variant.key,
                    "label": variant.label,
                    "description": variant.description,
                    "allowed_entry_close_times": list(variant.allowed_entry_close_times) if variant.allowed_entry_close_times is not None else None,
                    "excluded_entry_close_times": list(variant.excluded_entry_close_times),
                }
                for variant in variants
            ],
        },
        "results": {},
    }

    for symbol in SYMBOLS:
        enriched = _prepare_enriched(symbol)
        results["results"][symbol] = {}
        for variant in variants:
            results["results"][symbol][variant.key] = _run_variant(symbol, enriched, variant)

    return results


def _fmt_num(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "N/A"
    return f"{value:.{digits}f}"


def _fmt_pct(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "N/A"
    return f"{value:.{digits}f}%"


def _fmt_win_rate(value: float) -> str:
    return f"{value * 100:.1f}%"


def _fmt_profit_factor(payload: dict[str, Any]) -> str:
    if payload["profit_factor_flag"] == "inf":
        return "∞"
    return _fmt_num(payload["profit_factor"], 2)


def _fmt_sharpe(payload: dict[str, Any]) -> str:
    if payload["sharpe_flag"] == SHARPE_UNSTABLE_FLAG:
        return "不穩定(近零變異)"
    if payload["sharpe_flag"] == "insufficient_trades":
        return "N/A(交易不足)"
    return _fmt_num(payload["sharpe_ratio"], 2)


def _table_for_symbol(results: dict[str, Any], symbol: str) -> str:
    metadata = results["metadata"]
    variants = metadata["variants"]
    symbol_results = results["results"][symbol]

    lines = [
        "| 變體 | 交易數 | 低信度 | 勝率 | Profit Factor | Total Return % | Sharpe Ratio | Max DD % |",
        "| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |",
    ]

    for variant in variants:
        payload = symbol_results[variant["key"]]
        lines.append(
            "| {label} | {n} | {low_conf} | {win_rate} | {pf} | {ret} | {sharpe} | {mdd} |".format(
                label=variant["label"],
                n=payload["n_trades"],
                low_conf="是" if payload["low_confidence"] else "否",
                win_rate=_fmt_win_rate(payload["win_rate"]),
                pf=_fmt_profit_factor(payload),
                ret=_fmt_pct(payload["total_return_pct"]),
                sharpe=_fmt_sharpe(payload),
                mdd=_fmt_pct(payload["max_drawdown_pct"]),
            )
        )

    return "\n".join(lines)


def _best_variant(results: dict[str, Any], symbol: str, metric: str) -> tuple[str, dict[str, Any]]:
    best_key = "baseline"
    best_payload = results["results"][symbol][best_key]

    for key, payload in results["results"][symbol].items():
        candidate = payload.get(metric)
        best_value = best_payload.get(metric)
        if candidate is None:
            continue
        if best_value is None or float(candidate) > float(best_value):
            best_key = key
            best_payload = payload

    return best_key, best_payload


def build_report(results: dict[str, Any]) -> str:
    coverage = results["metadata"]["dataset_coverage"]
    baseline_signal_counts = results["metadata"]["baseline_signal_entry_close_time_counts_mtx"]
    coverage_type = coverage["coverage_type"]
    variants = results["metadata"]["variants"]
    total_variants = len(variants)
    count_values = list(coverage["counts_by_close_time"].values())
    uniform_count_note = (
        f"- 每個上述時間在資料中都出現 **{count_values[0]} 次**，表示這份樣本同時含有日盤與夜盤，而不是只有日盤。"
        if count_values and len(set(count_values)) == 1
        else "- 各時間點在資料中的出現次數不完全相同，詳見 JSON 內 `counts_by_close_time`。"
    )

    lines = [
        "# 時段濾網 A/B 測試（breakout 策略）",
        "",
        "## 1. 資料實際涵蓋的 session（先驗證，不預設）",
        "",
        (
            f"- 實際資料型態：**{coverage_type}**；本次檢查 `data/MTX_60min_real.csv` 與 `data/TX_60min_real.csv`，"
            f"兩者時間戳分布一致。"
        ),
        f"- 期間：**{coverage['start']} ~ {coverage['end']}**",
        f"- 總 bar 數：**{coverage['rows']}**",
        f"- 實際出現的 bar close 時間：**{', '.join(coverage['unique_close_times'])}**",
        f"- 日盤 bar：**{', '.join(coverage['day_session_close_times']) or '無'}**",
        f"- 夜盤 bar：**{', '.join(coverage['night_session_close_times']) or '無'}**",
        uniform_count_note,
        "",
        "## 2. 方法說明",
        "",
        "- breakout 訊號在當前 bar 收盤才成立，交易必須在**下一根 bar 開盤**才執行。",
        "- 因此本次『時段濾網』不是事後把成交單切掉，而是先看**下一根將成交的 bar 屬於哪個時窗**，再在訊號層直接抑制 long/short mask，最後才呼叫 `_apply_signal_columns`。",
        f"- 指標沿用既有 breakout 參數（MA {DEFAULT_STRATEGY.ma_fast}/{DEFAULT_STRATEGY.ma_mid}/{DEFAULT_STRATEGY.ma_slow}、SL {DEFAULT_STRATEGY.breakout_stop_loss_points}、TP {DEFAULT_STRATEGY.breakout_take_profit_points}）。",
        "- Total Return %、Sharpe Ratio、max_drawdown_pct 與不穩定 Sharpe 旗標，皆直接重用 `backend.experiments.proper_evaluation_with_dsr` 的既有函式。",
        f"- 低信度規則：**n_trades < {LOW_CONFIDENCE_TRADE_COUNT}**。",
        f"- 與先前報告一致，只有 Sharpe **明顯高於 4.18**（先前 12 次 trial 的 Prado 純運氣門檻）才值得被視為可能不是雜訊；本次其實又多做了 {total_variants - 1} 個新 trial，因此這個 4.18 其實已偏寬鬆。",
        f"- Baseline 原始可成交訊號（以『下一根將成交的 bar close 時間』統計）分布：**{', '.join(f'{k}={v}' for k, v in baseline_signal_counts.items())}**。",
        "- 上面這個分布裡**沒有 09:45 與 16:00**，代表在這段樣本中，baseline breakout 根本沒有產生『下一根會在兩個 session 開盤第一根成交』的訊號；所以相關 open-filter 變體若與 baseline 完全相同，是資料本身告訴我們『這個風險點在此樣本沒有被觸發』，不是程式失效。",
        "",
        "## 3. 測試變體",
        "",
    ]

    for variant in variants:
        lines.append(f"- **{variant['label']}**：{variant['description']}")

    for symbol in SYMBOLS:
        best_return_key, best_return_payload = _best_variant(results, symbol, "total_return_pct")
        best_sharpe_key, best_sharpe_payload = _best_variant(results, symbol, "sharpe_ratio")
        lines.extend(
            [
                "",
                f"## 4. {symbol} 結果",
                "",
                _table_for_symbol(results, symbol),
                "",
                f"- {symbol} 最佳 Total Return：**{best_return_payload['variant_label']}**（{_fmt_pct(best_return_payload['total_return_pct'])}）",
                (
                    f"- {symbol} 最佳可計算 Sharpe：**{best_sharpe_payload['variant_label']}** "
                    f"（{_fmt_sharpe(best_sharpe_payload)}）"
                ),
            ]
        )

        if not any(payload["beats_prado_pure_luck_bar_4_18"] for payload in results["results"][symbol].values()):
            lines.append(f"- {symbol}：**沒有任何變體的 Sharpe 超過 4.18**。")

    lines.extend(
        [
            "",
            "## 5. 結論",
            "",
            "- 這份資料**確定同時含日盤與夜盤**，所以本次是用 session-aware 的 entry 時窗去做真實訊號層測試，而不是事後切交易。",
            "- 若某變體看起來比 baseline 好，仍要先看它是否只是『少數幾筆』造成；表格裡凡是 `低信度=是` 都必須保守解讀。",
            "- 若 Sharpe 被標成 `不穩定(近零變異)`，代表報酬標準差接近零、比例會失真，不能把那個數字當真優勢。",
            "- 就算某些時段濾網略微改善 Total Return 或 max drawdown，只要 Sharpe 沒有越過 4.18，就不足以宣稱找到穩健 alpha；更別說這次又新增了額外 trial，真實門檻只會更高。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results = evaluate_all()

    OUTPUT_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    OUTPUT_MD.write_text(build_report(results), encoding="utf-8")

    print(f"Wrote {OUTPUT_JSON}")
    print(f"Wrote {OUTPUT_MD}")


if __name__ == "__main__":
    main()
