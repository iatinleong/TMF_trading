"""scripts/repair_live_kline.py — 一鍵自動線上修復/重新回補指定或最新收盤 K 棒。

使用方式：
  python scripts/repair_live_kline.py               # 自動修復最新一根收盤 K 棒（微台當前近月）
  python scripts/repair_live_kline.py --product TM2609 --bar 1788426000
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 加入專案根目錄至 sys.path
root = Path(__file__).resolve().parent.parent
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from backend.kline_engine import get_store
from backend.shioaji_backfill import backfill_via_shioaji
from backend.strategy_service import resolve_strategy_product_code


def main() -> None:
    parser = argparse.ArgumentParser(description="一鍵自動線上修復殘缺 K 棒並由永豐 Shioaji 回補完整歷史逐筆。")
    parser.add_argument("--product", default="", help="商品代號（留空自動帶入當前近月微台，如 TM2609）")
    parser.add_argument("--bar", type=int, default=None, help="指定要刪除修復的 K 棒 unix 秒數（留空預設為最新收盤那根）")
    args = parser.parse_args()

    prod = resolve_strategy_product_code(args.product)
    store = get_store(prod)

    removed_key = None
    if args.bar:
        if args.bar in store._bars:
            store._bars.pop(args.bar, None)
            removed_key = args.bar
    else:
        if store._bars:
            removed_key = max(store._bars.keys())
            store._bars.pop(removed_key, None)

    if removed_key is not None:
        store._persist()
        print(f"[1/2] 已從記憶體與磁碟快取清除殘缺 K 棒 (key={removed_key})")
    else:
        print("[1/2] 快取中無 K 棒可清除，直接執行回補檢查")

    print(f"[2/2] 開始由永豐 Shioaji 查詢完整逐筆資料回補 {prod}...")
    added = backfill_via_shioaji(prod, lookback_days=1)
    print(f"✓ 修復完成！已由 Shioaji 成功回補 {added} 根 K 棒！")


if __name__ == "__main__":
    main()
