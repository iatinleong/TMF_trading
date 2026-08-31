# -*- coding: utf-8 -*-
"""把某個策略綁定給某個帳號——工程操作，不透過 Dashboard 網頁（見
docs/superpowers/plans/2026-08-26-account-strategy-pairing.md）。

「客製化」指的是你幫這個客戶另外寫了一份策略程式碼（不同的進出場邏輯），
部署到這台機器之後，用這支腳本把那個帳號（Supabase user id）跟這個
strategy_id 的對照關係寫進資料庫。使用者登入 Dashboard 之後，右側「自動
策略」面板只會讀這張表、顯示綁定給他的策略，不會自己新增/改參數。

用法（本專案根目錄執行，需要 .env 裡有 SUPABASE_URL/SUPABASE_SECRET_KEY）:
  python scripts/bind_strategy.py --list-strategies
  python scripts/bind_strategy.py --user-id <uuid> --strategy-id breakout_long --product-code TM2608 --qty 1
  python scripts/bind_strategy.py --user-id <uuid> --strategy-id breakout_long --enabled
"""
from __future__ import annotations

# 這台機器的 certifi 憑證清單抓不到 Supabase 的憑證鏈（見 run_server.py 同樣的
# 註解），要用 truststore 改成信任 Windows 系統憑證存放區，且必須在任何模組
# 建立 SSL 連線之前注入，所以放在所有其他 import 之前。
import truststore

truststore.inject_into_ssl()

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent.parent
load_dotenv(BASE / ".env")
sys.path.insert(0, str(BASE))

if sys.platform == "win32":
    # 這台機器的預設主控台編碼不是 UTF-8，中文輸出會變亂碼（跟
    # scripts/run_once_trace.py 踩過的同一個問題）。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

from backend.api import append_action_audit  # noqa: E402
from backend.strategy_config_store import admin_upsert_strategy_config  # noqa: E402
from backend.strategy_service import STRATEGY_DEFS  # noqa: E402


def _print_available_strategies() -> None:
    print("目前可綁定的 strategy_id：")
    for strategy_id, defs in STRATEGY_DEFS.items():
        print(f"  {strategy_id}\t{defs['label']}（{defs['strategy']} / {defs['direction_limit']}）")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-strategies", action="store_true", help="列出目前可綁定的 strategy_id")
    parser.add_argument("--user-id", help="Supabase auth.users.id (UUID)")
    parser.add_argument("--strategy-id", help="策略識別碼")
    parser.add_argument("--product-code", help="覆蓋交易商品（例如 TM2608）")
    parser.add_argument("--qty", type=int, help="覆蓋委託口數（例如 1 或 2）")
    parser.add_argument("--enabled", dest="enabled", action="store_true", default=None, help="啟用此策略")
    parser.add_argument("--disabled", dest="enabled", action="store_false", help="停用此策略")
    parser.add_argument("--stop-loss-points", type=float, help="覆蓋停損點數（例如 80）")
    parser.add_argument("--take-profit-points", type=float, help="覆蓋停利點數（例如 200）")
    parser.add_argument("--max-loss-ntd", type=float, help="覆蓋單日最大虧損 NTD（例如 5000）")
    parser.add_argument("--max-loss-pct", type=float, help="覆蓋單日最大虧損比例（例如 0.05）")
    parser.add_argument("--oco-enabled", dest="oco_enabled", action="store_true", default=None, help="開啟 Layer 1 OCO 智慧單")
    parser.add_argument("--no-oco-enabled", dest="oco_enabled", action="store_false", help="關閉 Layer 1 OCO 智慧單")
    parser.add_argument("--soft-stop-enabled", dest="soft_stop_enabled", action="store_true", default=None, help="開啟 Layer 2 軟停損停利")
    parser.add_argument("--no-soft-stop-enabled", dest="soft_stop_enabled", action="store_false", help="關閉 Layer 2 軟停損停利")
    parser.add_argument("--risk-insurance-enabled", dest="risk_insurance_enabled", action="store_true", default=None, help="開啟 Layer 3 金額/比例硬停損")
    parser.add_argument("--no-risk-insurance-enabled", dest="risk_insurance_enabled", action="store_false", help="關閉 Layer 3 金額/比例硬停損")
    parser.add_argument("--reverse-signal-exit-enabled", dest="reverse_signal_exit_enabled", action="store_true", default=None, help="開啟 Layer 4 反向訊號出場")
    parser.add_argument("--no-reverse-signal-exit-enabled", dest="reverse_signal_exit_enabled", action="store_false", help="關閉 Layer 4 反向訊號出場")
    args = parser.parse_args()

    if args.list_strategies:
        _print_available_strategies()
        return 0

    if not args.user_id or not args.strategy_id:
        parser.error("--user-id 和 --strategy-id 都是必填（或改用 --list-strategies）")

    if args.strategy_id not in STRATEGY_DEFS:
        print(f"未知的 strategy_id: {args.strategy_id}", file=sys.stderr)
        _print_available_strategies()
        return 1

    row = admin_upsert_strategy_config(
        args.user_id,
        args.strategy_id,
        product_code=args.product_code,
        qty=args.qty,
        enabled=args.enabled,
        stop_loss_points=args.stop_loss_points,
        take_profit_points=args.take_profit_points,
        max_loss_ntd=args.max_loss_ntd,
        max_loss_pct=args.max_loss_pct,
        oco_enabled=args.oco_enabled,
        soft_stop_enabled=args.soft_stop_enabled,
        risk_insurance_enabled=args.risk_insurance_enabled,
        reverse_signal_exit_enabled=args.reverse_signal_exit_enabled,
    )
    append_action_audit(
        action="BIND_STRATEGY_CONFIG",
        user_id=args.user_id,
        payload={
            "strategy_id": args.strategy_id,
            "product_code": args.product_code,
            "qty": args.qty,
            "enabled": args.enabled,
            "stop_loss_points": args.stop_loss_points,
            "take_profit_points": args.take_profit_points,
            "max_loss_ntd": args.max_loss_ntd,
            "max_loss_pct": args.max_loss_pct,
            "oco_enabled": args.oco_enabled,
            "soft_stop_enabled": args.soft_stop_enabled,
            "risk_insurance_enabled": args.risk_insurance_enabled,
            "reverse_signal_exit_enabled": args.reverse_signal_exit_enabled,
        },
        status="SUCCESS",
    )
    print(f"已綁定：{row}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
