from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

try:
    from .backtest_engine import run_backtest, trades_to_dataframe
    from .config import DEFAULT_STRATEGY
    from .indicators import add_moving_averages, resample_to_60min
    from .signals import generate_breakout_signals, generate_pullback_signals
except ImportError:  # pragma: no cover - script execution fallback
    from backtest_engine import run_backtest, trades_to_dataframe  # type: ignore
    from config import DEFAULT_STRATEGY  # type: ignore
    from indicators import add_moving_averages, resample_to_60min  # type: ignore
    from signals import generate_breakout_signals, generate_pullback_signals  # type: ignore


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Taiwan Index Futures 60-minute backtest.")
    parser.add_argument("--data", required=True, help="Path to CSV with datetime,open,high,low,close,volume columns.")
    parser.add_argument("--strategy", required=True, choices=("breakout", "pullback"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    data_path = Path(args.data)

    raw = pd.read_csv(data_path)
    bars_60m = resample_to_60min(raw)
    enriched = add_moving_averages(
        bars_60m,
        fast=DEFAULT_STRATEGY.ma_fast,
        mid=DEFAULT_STRATEGY.ma_mid,
        slow=DEFAULT_STRATEGY.ma_slow,
    )

    if args.strategy == "breakout":
        signaled = generate_breakout_signals(enriched)
    else:
        signaled = generate_pullback_signals(enriched)

    trades, summary = run_backtest(signaled, strategy=args.strategy)
    trades_path = data_path.with_name(f"{data_path.stem}_trades.csv")
    trades_to_dataframe(trades).to_csv(trades_path, index=False)

    print(summary)
    print(f"Trade log written to: {trades_path}")


if __name__ == "__main__":
    main()
