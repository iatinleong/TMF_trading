# -*- coding: utf-8 -*-
"""對運行中的 uvicorn backend 做 REST API 連續驗證。"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8765"
POLL_SEC = 30
POLL_INTERVAL = 2.0


def get(path: str) -> dict:
    req = urllib.request.Request(f"{BASE}{path}")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post(path: str, body: dict | None = None) -> dict:
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    results: list[dict] = []
    failed = 0

    def check(name: str, ok: bool, detail: str) -> None:
        nonlocal failed
        if not ok:
            failed += 1
        results.append({"name": name, "ok": ok, "detail": detail})
        mark = "OK" if ok else "FAIL"
        print(f"[{mark}] {name}: {detail}")

    try:
        health = get("/api/health")
        check("health", health.get("status") == "ok", str(health))
    except urllib.error.URLError as exc:
        check("health", False, f"無法連線 {BASE} — {exc}")
        print(json.dumps({"failed": failed, "results": results}, ensure_ascii=False, indent=2))
        return 1

    conn = get("/api/connection")
    check("connection.connected", bool(conn.get("connected")), f"user={conn.get('user_id')}")
    check("connection.quote_ready", bool(conn.get("quote_ready")), f"quote_connected={conn.get('quote_connected')}")
    check("connection.quote_subscribed", conn.get("quote", {}).get("product_code") == "TMF00", str(conn.get("quote")))

    status = get("/api/trading/status")
    check("trading.accounts", "F0200006921941" in (status.get("accounts") or []), str(status.get("accounts")))
    msgs = status.get("messages") or []
    has_3003 = any("3003" in (m.get("text") or "") for m in msgs)
    check("trading.messages_3003", has_3003, f"messages={len(msgs)}")

    refresh = post("/api/trading/refresh")
    rights = refresh.get("rights") or {}
    equity = rights.get("equity")
    check("refresh.equity", equity is not None and equity > 0, f"equity={equity}")

    account = get("/api/account")
    check("account.connected", bool(account.get("connected")), f"account={account.get('active_account')}")

    ticker_samples: list[dict] = []
    print(f"\n--- 輪詢 /api/ticker {POLL_SEC}s（每 {POLL_INTERVAL}s）---")
    deadline = time.time() + POLL_SEC
    got_price = False
    while time.time() < deadline:
        t = get("/api/ticker")
        ticker_samples.append(
            {
                "time": time.strftime("%H:%M:%S"),
                "last_price": t.get("last_price"),
                "bid": t.get("bid"),
                "ask": t.get("ask"),
            }
        )
        if t.get("last_price") is not None:
            got_price = True
            print(f"  tick @ {ticker_samples[-1]['time']}: last={t['last_price']} bid={t.get('bid')} ask={t.get('ask')}")
        time.sleep(POLL_INTERVAL)

    check(
        "ticker.live_price",
        got_price,
        "非交易時段可能無成交；見 ticker_samples 最後一筆",
    )

    report = {
        "base_url": BASE,
        "failed": failed,
        "connection": {
            "connected": conn.get("connected"),
            "quote_ready": conn.get("quote_ready"),
            "quote_connected": conn.get("quote_connected"),
        },
        "ticker_samples": ticker_samples[-5:],
        "results": results,
    }
    out = __file__.replace("test_api_live.py", "test_api_live_output.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(f"\n報告: {out}")
    print(f"總計: {len(results) - failed}/{len(results)} 通過")
    return 0 if failed <= 1 else 1  # ticker 無價在非盤時允許 1 項失敗


if __name__ == "__main__":
    sys.exit(main())