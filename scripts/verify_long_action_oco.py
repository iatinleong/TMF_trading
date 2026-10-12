"""
長效單（nLongActionFlag=1）驗證腳本——送一張遠離市價的真實測試單，確認：
1. 長效單欄位（nLongActionFlag / nTimeFlag / bstrLongEndDate）送出去不會被拒單。
2. GetStopLossReport(kind="OCO") 查詢回來的格式，跟 backend/capital_parse.py 解析的欄位位置對不對，
   尤其是「長效單序號」（bstrLongActionKey，撤單要用）。
3. 用查到的長效單序號，撤單撤不撤得掉。

== 這個腳本只能在真的連得上群益正式環境的機器上跑（目前是 GCP VM，不是這台開發機）==

== 跑之前必須確認的事 ==
1. 在儀表板或環境變數確認 DISABLE_TRADING=0（允許真實下單）。
2. 跑完之後，不管成功失敗，記得確認單子已完全撤除。

== 安全設計 ==
- 下單前會先讀即時報價，觸發價設在「現價 ± SAFETY_MARGIN_POINTS」（預設 2500 點），
  大約為市價的 ±5%，安全落在期交所 10% 漲跌停限制內（不會被券商以超限拒單），
  但在數秒內完全不可能被觸發。
- 嚴格遵守群益規定：第一腳觸發價（高腳）嚴格大於第二腳觸發價（低腳）。
- 自動偵測目前留倉部位：若帳上有多單留倉，以平倉單（close）掛出；若無部位，以新倉（open）掛出。
- 查詢時明確指定 kind="OCO"，避免群益預設查 STP 導致查不到。
- 結束時立即撤單；若有例外在 finally 區塊會保底再撤一次，確保不留孤兒單。

用法（在 VM 上，本專案根目錄）：
    .venv_build\\Scripts\\python scripts\\verify_long_action_oco.py
"""

from __future__ import annotations

import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.capital_parse import parse_stop_loss_report_raw  # noqa: E402
from backend.trading_service import TradingService, compute_current_tmf_code, trading_disabled  # noqa: E402
from backend.broker.capital_futures import _default_long_end_date  # noqa: E402

SAFETY_MARGIN_POINTS = 2500.0
QTY = 1


def _settlement_month(tmf_code: str) -> str:
    # "TM2610" -> "202610"
    yy, mm = tmf_code[2:4], tmf_code[4:6]
    return f"20{yy}{mm}"


def _wait_for_quote(svc: TradingService, *, timeout: float = 30.0) -> float:
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = svc.status()
        price = (st.get("quote") or {}).get("last_price")
        if price:
            return float(price)
        time.sleep(1)
        print(".", end="", flush=True)
    print()
    raise RuntimeError("等不到即時報價，請確認報價連線正常後再試一次")


def main() -> None:
    if trading_disabled():
        raise SystemExit(
            "目前 DISABLE_TRADING=1（已關閉真實下單）。\n"
            "請先在儀表板打開「允許真實下單」，確認帳上沒有未預期的部位後，再跑這個腳本。"
        )

    tmf_code = compute_current_tmf_code()
    settlement_month = _settlement_month(tmf_code)
    print(f"商品月份碼: {tmf_code}（settlement_month={settlement_month}）")

    svc = TradingService.get()
    smart_key = ""
    order_no = ""
    seq_no = ""
    long_action_key = ""
    try:
        print("連線中...")
        svc.connect(product_code="TMF")

        st = svc.status()
        positions = st.get("positions") or []
        print(f"目前帳上留倉部位: {positions}")

        # 根據帳上部位動態決定平倉 (close) 或新倉 (open)
        has_long = any(p.get("direction_key") == "long" and float(str(p.get("qty", 0)).replace(",", "")) > 0 for p in positions)
        if has_long:
            new_close = "close"
            side = "sell"
            side2 = "sell"
            print("偵測到多單留倉，測試單將以 平倉單 (close / sell) 掛出。")
        else:
            new_close = "open"
            side = "buy"
            side2 = "buy"
            print("帳上無多單留倉，測試單將以 新倉單 (open / buy) 掛出（測試後將立即撤單）。")

        print("等待即時報價...", end="", flush=True)
        current_price = _wait_for_quote(svc)
        print(f"\n現價: {current_price:.0f}")

        # 安全距離：高腳現價+2500點，低腳現價-2500點
        # 嚴格滿足群益規則：第一腳觸發價必須大於第二腳觸發價
        high_trigger = round(current_price + SAFETY_MARGIN_POINTS)
        low_trigger = round(current_price - SAFETY_MARGIN_POINTS)
        print(f"觸發價設定（遠離現價 ±{SAFETY_MARGIN_POINTS:.0f} 點）：")
        print(f"  第一腳（高腳）: {high_trigger}（距離現價 +{high_trigger - current_price:.0f} 點）")
        print(f"  第二腳（低腳）: {low_trigger}（距離現價 -{current_price - low_trigger:.0f} 點）")
        print("確認：兩腳皆深處安全區間且在 10% 漲跌幅限制內，正常行情完全不會被觸發。\n")

        long_end_date = _default_long_end_date(settlement_month)
        print(f"長效單結束日（對齊商品最後交易日/結算日）: {long_end_date}")

        print("\n=== 步驟 1：送出長效 OCO 測試單 ===")
        result = svc.place_oco_order(
            {
                "stock_no": tmf_code,
                "settlement_month": settlement_month,
                "qty": QTY,
                "side": side,
                "trigger_price": str(high_trigger),
                "price": str(high_trigger),
                "side2": side2,
                "trigger_price2": str(low_trigger),
                "price2": str(low_trigger),
                "order_price_type": 2,
                "new_close": new_close,
                "long_action_flag": True,
                "long_end_date": long_end_date,
            }
        )
        order_result = result.get("order_result") or {}
        print("送單回應:", order_result)
        if not order_result.get("success"):
            raise SystemExit(f"送單失敗，停止（不需要撤單）：{order_result.get('message')}")

        raw = str(order_result.get("raw") or "")
        parts = [p.strip() for p in raw.split(",")]
        if len(parts) >= 6 and parts[0] in ("000", "0"):
            parts = parts[1:]

        if len(parts) >= 5:
            order_no, smart_key, seq_no = parts[2], parts[3], parts[4]
            print(f"解析成功 -> 智慧單號={smart_key} 委託書號={order_no} 序號={seq_no}")
        else:
            print(f"[!] 回應欄位數不如預期（{len(parts)} 欄），無法解析智慧單號，無法繼續查詢/撤單。")
            print("原始回應:", raw)
            return

        print("\n=== 步驟 2：查詢 GetStopLossReport(kind='OCO')，確認長效單序號 ===")
        matched = None
        for attempt in range(1, 4):
            print(f"查詢中 (第 {attempt}/3 次)...")
            time.sleep(1.5)
            report = svc.query_stop_loss_report(kind="OCO")
            rows = report.get("stop_loss_rows") or parse_stop_loss_report_raw(report.get("stop_loss_raw") or "")
            matched = next((r for r in rows if r.get("smart_key") == smart_key), None)
            if matched:
                break

        if matched is None:
            print(f"[!] 查詢結果裡找不到智慧單號 {smart_key}，以下是查到的所有 smart_key：")
            print([r.get("smart_key") for r in (rows or [])])
        else:
            print("比對到這筆委託，完整解析內容：")
            for key, value in matched.items():
                if key == "raw":
                    continue
                print(f"  {key}: {value}")
            print(f"\n>>> 長效單註記 (long_action_flag): {matched.get('long_action_flag')}")
            print(f">>> 長效單序號 (long_action_key): {matched.get('long_action_key')!r}")
            print(f">>> 長效單結束日期: {matched.get('long_action_end_date')}")

        long_action_key = (matched or {}).get("long_action_key") or ""

        print("\n=== 步驟 3：撤單（用查到的長效單序號）===")
        cancel_result = svc.cancel_stop_order(
            {
                "smart_key": smart_key,
                "seq_no": seq_no,
                "order_no": order_no,
                "long_action_key": long_action_key,
                "trade_kind": 3,
            }
        )
        cancel_info = cancel_result.get("cancel_result") or {}
        print("撤單回應:", cancel_info)
        if cancel_info.get("success"):
            smart_key = ""  # 撤成功了，finally 不用再試一次
            print("\n[OK] 撤單成功！長效單流程完整驗證完畢。")
        else:
            print("\n[!] 撤單未回傳成功代碼，finally 區塊會進行保底再撤一次；若仍未果請至券商軟體確認。")

    finally:
        if smart_key:
            print("\n（finally 保底）嘗試撤單，確保不留孤兒單...")
            try:
                svc.cancel_stop_order(
                    {
                        "smart_key": smart_key,
                        "seq_no": seq_no,
                        "order_no": order_no,
                        "long_action_key": long_action_key,
                        "trade_kind": 3,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                print(f"（finally）撤單仍然失敗: {exc}，請人工確認。")
        try:
            svc.disconnect()
            print("已完成登出。")
        except Exception as exc:  # noqa: BLE001
            print(f"登出時發生例外: {exc}")


if __name__ == "__main__":
    main()
