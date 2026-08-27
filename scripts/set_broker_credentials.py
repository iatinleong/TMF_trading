# -*- coding: utf-8 -*-
"""把某個登入帳號的群益期貨帳密存進資料庫，給該帳號自己的 worker 行程用
（見 docs/superpowers/plans/2026-08-26-per-account-worker-process.md）。

密碼一律用互動輸入（getpass），絕對不要當成命令列參數傳——命令列參數會
留在 shell history、`ps`/工作管理員的行程清單裡，等於把密碼寫在看得到
的地方。

用法（本專案根目錄執行，需要 .env 裡有 SUPABASE_URL/SUPABASE_SECRET_KEY）:
  python scripts/set_broker_credentials.py --user-id <supabase uuid> --capital-user-id A123456789
  （執行後會互動提示輸入群益交易密碼，不會顯示在螢幕上）
"""
from __future__ import annotations

# 這台機器的 certifi 憑證清單抓不到 Supabase 的憑證鏈（見 run_server.py 同樣的
# 註解），要用 truststore 改成信任 Windows 系統憑證存放區，且必須在任何模組
# 建立 SSL 連線之前注入，所以放在所有其他 import 之前。
import truststore

truststore.inject_into_ssl()

import argparse
import getpass
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent.parent
load_dotenv(BASE / ".env")
sys.path.insert(0, str(BASE))

if sys.platform == "win32":
    # 這台機器的預設主控台編碼不是 UTF-8，中文輸出會變亂碼（跟
    # scripts/run_once_trace.py、scripts/bind_strategy.py 踩過的同一個問題）。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

from backend.broker_credentials_store import admin_set_broker_credentials  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user-id", required=True, help="Supabase user id（uuid）")
    parser.add_argument("--capital-user-id", required=True, help="群益期貨帳號（身分證字號或期貨帳號）")
    args = parser.parse_args()

    password = getpass.getpass("群益交易密碼（輸入時不會顯示）：")
    if not password:
        print("密碼不能是空的", file=sys.stderr)
        return 1

    row = admin_set_broker_credentials(
        args.user_id, capital_user_id=args.capital_user_id, capital_password=password,
    )
    print(f"已儲存帳密：user_id={row['user_id']} capital_user_id={row['capital_user_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
