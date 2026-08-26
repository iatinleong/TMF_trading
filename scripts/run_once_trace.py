# -*- coding: utf-8 -*-
"""執行一次完整連線流程，輸出步驟、檔案路徑與 COM return value。"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent.parent
OUTPUT_JSON = BASE / "scripts" / "run_once_trace_output.json"
load_dotenv(BASE / ".env")
sys.path.insert(0, str(BASE))

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

from backend.broker.capital_futures import (  # noqa: E402
    CapitalFuturesBroker,
    Environment,
    _find_default_dll_path,
)
from backend.trading_service import (  # noqa: E402
    resolve_order_product_code,
    resolve_quote_product_code,
)

WAIT_QUOTE_TIMEOUT = 120.0


def _msg(center, code: int) -> str:
    try:
        return center.SKCenterLib_GetReturnCodeMessage(int(code))
    except Exception:
        return str(code)


def _step(steps: list, name: str, **fields) -> None:
    steps.append({"step": name, **fields})


def main() -> None:
    steps: list[dict] = []
    user_id = os.getenv("CAPITAL_USER_ID", "")
    password_set = bool(os.getenv("CAPITAL_PASSWORD"))
    env = int(os.getenv("CAPITAL_ENVIRONMENT", str(Environment.PRODUCTION)))
    product = os.getenv("LIVE_PRODUCT_CODE", "TMF00")
    account_env = os.getenv("CAPITAL_ACCOUNT") or None

    dll_path = Path(os.getenv("CAPITAL_DLL_PATH", str(_find_default_dll_path())))
    log_path = Path(os.getenv("CAPITAL_LOG_PATH", str(BASE / "data" / "capital_logs")))

    files = {
        "project_root": str(BASE),
        "env_file": str(BASE / ".env"),
        "skcom_dll": str(dll_path),
        "log_dir": str(log_path),
        "python_entry": str(Path(__file__).resolve()),
        "broker_module": str(BASE / "backend" / "broker" / "capital_futures.py"),
        "trading_service": str(BASE / "backend" / "trading_service.py"),
    }
    _step(
        steps,
        "0_config",
        files=files,
        env={
            "CAPITAL_USER_ID": user_id,
            "CAPITAL_PASSWORD": "***" if password_set else "(missing)",
            "CAPITAL_ENVIRONMENT": env,
            "LIVE_PRODUCT_CODE": product,
            "CAPITAL_ACCOUNT": account_env,
        },
        product_mapping={
            "input": product,
            "quote_code": resolve_quote_product_code(product),
            "order_code": resolve_order_product_code(product),
        },
    )

    def _write_report(payload: dict) -> None:
        with OUTPUT_JSON.open("w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"報告已寫入: {OUTPUT_JSON}")

    if not user_id or not password_set:
        _write_report({"error": "缺少 CAPITAL_USER_ID / CAPITAL_PASSWORD", "steps": steps})
        return

    t0 = time.time()
    broker = CapitalFuturesBroker(environment=env)
    _step(steps, "1_broker_init", return_value=0, message="CapitalFuturesBroker 建立完成", dll_used=str(broker.dll_path))

    log_path.mkdir(parents=True, exist_ok=True)
    log_dir = str(broker.log_path.resolve())
    code = int(broker.center.SKCenterLib_SetLogPath(log_dir))
    _step(
        steps,
        "2_SKCenterLib_SetLogPath",
        api="SKCenterLib_SetLogPath",
        args={"path": log_dir},
        return_code=code,
        return_message=_msg(broker.center, code),
    )

    code = int(broker.center.SKCenterLib_SetAuthority(env))
    _step(
        steps,
        "3_SKCenterLib_SetAuthority",
        api="SKCenterLib_SetAuthority",
        args={"environment": env},
        return_code=code,
        return_message=_msg(broker.center, code),
    )

    code = int(broker.center.SKCenterLib_Login(user_id, os.getenv("CAPITAL_PASSWORD", "")))
    _step(
        steps,
        "4_SKCenterLib_Login",
        api="SKCenterLib_Login",
        args={"user_id": user_id},
        return_code=code,
        return_message=_msg(broker.center, code),
    )
    if code != 0:
        _write_report({"elapsed_sec": round(time.time() - t0, 2), "steps": steps})
        return

    broker.user_id = user_id
    broker._logged_in = True
    broker.live.user_id = user_id
    broker.live.connected = True

    quote_product = resolve_quote_product_code(product)
    order_product = resolve_order_product_code(product)
    broker.live.order_product_code = order_product

    code = int(broker.quote.SKQuoteLib_EnterMonitorLONG())
    broker._quote_monitoring = code == 0
    _step(
        steps,
        "5_SKQuoteLib_EnterMonitorLONG",
        api="SKQuoteLib_EnterMonitorLONG",
        note="對齊 PythonExampleV2：登入後先連報價，下單初始化延後",
        return_code=code,
        return_message=_msg(broker.center, code),
    )

    wait_start = time.time()
    quote_ready = broker._wait_quote_connected(timeout=WAIT_QUOTE_TIMEOUT)
    quote_events = [m for m in broker.live.messages if m["category"] == "quote_conn"]
    _step(
        steps,
        "6_wait_OnConnection_3003",
        api="PumpWaitingMessages + OnConnection(3003)",
        timeout_sec=WAIT_QUOTE_TIMEOUT,
        quote_ready=quote_ready,
        elapsed_sec=round(time.time() - wait_start, 2),
        final_IsConnected=int(broker.quote.SKQuoteLib_IsConnected()),
        quote_conn_events=quote_events,
    )

    subscribe_ok = False
    tick_code = None
    list_code = None
    if broker.is_quote_ready():
        list_code = int(broker.quote.SKQuoteLib_RequestStockList(2))
        broker._pump_events(2.0)
        tick_code = broker.request_ticks(quote_product)
        broker._snapshot_quote(quote_product)
        broker._pump_events(2.0)
        subscribe_ok = tick_code == 0
        _step(
            steps,
            "7_subscribe_quote",
            api="RequestStockList(2) + RequestTicks",
            args={"quote_product": quote_product},
            RequestStockList_return=list_code,
            RequestStockList_message=_msg(broker.center, list_code),
            RequestTicks_return=tick_code,
            RequestTicks_message=_msg(broker.center, tick_code),
            subscribe_ok=subscribe_ok,
        )
    else:
        _step(
            steps,
            "7_subscribe_quote",
            skipped=True,
            reason="3003 未到，略過 RequestStockList/RequestTicks",
        )

    code = int(broker.order.SKOrderLib_Initialize())
    _step(
        steps,
        "8_SKOrderLib_Initialize",
        api="SKOrderLib_Initialize",
        return_code=code,
        return_message=_msg(broker.center, code),
    )

    cert_code = int(broker.order.ReadCertByID(user_id))
    _step(
        steps,
        "9_ReadCertByID",
        api="ReadCertByID",
        args={"user_id": user_id},
        return_code=cert_code,
        return_message=_msg(broker.center, cert_code),
    )
    if cert_code != 0:
        _write_report({"elapsed_sec": round(time.time() - t0, 2), "steps": steps})
        return

    broker.order.GetUserAccount()
    broker._pump_events(5.0)
    broker._order_initialized = True
    broker.live.accounts = list(broker.accounts)
    if broker.accounts and not broker.live.active_account:
        futures_accounts = [a for a in broker.accounts if a.startswith("F")]
        broker.live.active_account = futures_accounts[0] if futures_accounts else broker.accounts[0]
    if account_env:
        broker.live.active_account = account_env
    _step(
        steps,
        "10_GetUserAccount",
        api="GetUserAccount + PumpEvents(5s)",
        accounts=broker.accounts,
        active_account=broker.live.active_account,
    )

    code = int(broker.center.SKCenterLib_RequestAgreement(user_id))
    broker._pump_events(2.0)
    broker._evaluate_kline_api_agreement()
    _step(
        steps,
        "11_SKCenterLib_RequestAgreement",
        api="SKCenterLib_RequestAgreement",
        args={"user_id": user_id},
        return_code=code,
        return_message=_msg(broker.center, code),
        agreements=broker.live.agreements,
        kline_api_ok=broker.live.kline_api_ok,
        kline_api_block_reason=broker.live.kline_api_block_reason,
    )

    if broker.live.active_account:
        broker.refresh_live_snapshot(broker.live.active_account, force=True)
    _step(
        steps,
        "12_refresh_live_snapshot",
        api="GetOrderReport + GetFulfillReport + GetOpenInterestGW + GetFutureRights",
        active_account=broker.live.active_account,
    )

    final_state = broker.get_live_state()
    log_files = []
    if log_path.exists():
        for p in sorted(log_path.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True):
            if p.is_file():
                log_files.append({
                    "name": p.name,
                    "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime)),
                    "size": p.stat().st_size,
                })

    report = {
        "elapsed_sec": round(time.time() - t0, 2),
        "flow_summary": [
            "SetLogPath → SetAuthority → Login",
            "EnterMonitorLONG → 等 3003 → RequestStockList/RequestTicks",
            "SKOrderLib_Initialize → ReadCertByID → GetUserAccount",
            "RequestAgreement → refresh_live_snapshot",
        ],
        "steps": steps,
        "final_state": final_state,
        "log_files_written": log_files[:12],
    }
    _write_report(report)


if __name__ == "__main__":
    main()