"""群益 API 回報字串解析（委託 / 持倉 / 權益 / K 線）。"""

from __future__ import annotations

import re
from typing import Any

import pandas as pd

from .timeutil import to_unix_seconds

_FUTURE_RIGHTS_FIELDS = (
    "account_balance",
    "floating_pl",
    "realized_fee",
    "transaction_tax",
    "withheld_premium",
    "premium_payment",
    "equity",
    "excess_margin",
    "deposit_withdrawal",
    "buyer_market_value",
    "seller_market_value",
    "futures_close_pl",
    "intraday_unrealized",
    "initial_margin",
    "maintenance_margin",
    "position_initial_margin",
    "position_maintenance_margin",
    "order_margin",
    "excess_optimal_margin",
    "total_premium_value",
    "withheld_fee",
    "original_initial_margin",
    "yesterday_balance",
    "option_combo_margin_flag",
    "maintenance_rate",
    "currency",
    "full_initial_margin",
    "full_maintenance_margin",
    "full_available",
    "offset_amount",
    "securities_available",
    "available_balance",
    "full_cash_available",
    "securities_value",
    "risk_indicator",
    "option_expiry_difference",
    "option_expiry_pl",
    "futures_expiry_pl",
    "additional_margin",
    "login_id",
    "account_no",
)


def _normalize_direction(value: str) -> str:
    text = str(value or "").strip().upper()
    if text in {"B", "BUY", "0", "買", "買進", "多", "LONG"}:
        return "long"
    if text in {"S", "SELL", "1", "賣", "賣出", "空", "SHORT"}:
        return "short"
    return text or "—"


def _direction_label(direction: str) -> str:
    if direction == "long":
        return "多"
    if direction == "short":
        return "空"
    return direction or "—"


def parse_open_interest_line(line: str) -> dict[str, Any]:
    """
    解析 OnOpenInterest（GetOpenInterestGW）未平倉單行 CSV。
    官方文件《7.下單-國內期選.docx》OnOpenInterest 欄位定義（格式1，逐筆）：
    0市場別 1帳號 2商品 3買賣別 4未平倉部位 5當沖未平倉部位 6平均成本 7單口手續費 8交易稅 9LOGIN_ID
    查詢結束會多送一筆以「##」開頭的終止行；查無資料回傳「001,查無資料」。
    """
    text = line.strip()
    if not text or text.startswith("##"):
        return {}
    parts = [p.strip() for p in text.split(",")]
    if any("查無" in p or "錯誤" in p for p in parts):
        return {}
    if len(parts) < 7:
        return {}

    row: dict[str, Any] = {"raw": text}
    row["market"] = parts[0]
    row["account"] = parts[1]
    row["product"] = parts[2]
    row["direction"] = _direction_label(_normalize_direction(parts[3]))
    row["direction_key"] = _normalize_direction(parts[3])
    row["qty"] = parts[4]
    row["daytrade_qty"] = parts[5]
    row["price"] = parts[6]
    return row


def parse_open_interest_raw(raw: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in str(raw or "").splitlines():
        if line.strip():
            parsed = parse_open_interest_line(line)
            if parsed:
                rows.append(parsed)
    return rows


_ORDER_STATUS_LABELS = {
    "0": "預約單",
    "2": "全部成交",
    "3": "全部取消",
    "4": "部分成交，剩餘已取消",
    "5": "部分成交，剩餘可取消",
    "6": "委託失敗",
    "7": "委託成功（待成交）",
    "8": "取消失敗",
    "9": "取消中",
    "F1": "動態退單－全部取消",
    "F2": "動態退單－部分成交剩餘已取消",
    "F3": "動態退單－部分委託成功",
    "F4": "動態退單－否決",
}

# 有效（掛單中／可撤單）的委託狀態代碼
_ORDER_ACTIVE_STATUS_CODES = {"0", "5", "7", "8", "9", "F3"}


def _at(parts: list[str], idx: int) -> str:
    return parts[idx] if len(parts) > idx else ""


def parse_order_report_line(line: str) -> dict[str, Any]:
    """
    解析委託回報單行（GetOrderReport）。
    官方文件《4.下單準備介紹.docx》GetOrderReport 欄位定義（證期委託查詢回報格式），
    已用真實 orders_raw 資料逐欄核對過（2026-07-27）：
    0市場別 1商品別 2交易所別 3分公司代號 4IBNO 5交易帳號 6子帳號 7委託書號
    8委託13碼流水序號(SeqNo) 9原始13碼流水序號 10委託狀態(數字代碼，非中文)
    11委託日期 12委託時間 15商品代號 22買賣別(B/S) 26委託方式 27委託價 28原始委託價
    30原始委託數量 31成交數量 32剩餘數量 33當沖註記(Y當沖/N新倉/O平倉/A自動)
    43成交均價
    """
    text = line.strip()
    if not text:
        return {}
    parts = [p.strip() for p in text.split(",")]
    if len(parts) < 34:
        return {"raw": text}

    row: dict[str, Any] = {"raw": text}
    row["book_no"] = _at(parts, 7)
    row["seq_no"] = _at(parts, 8)
    row["orig_seq_no"] = _at(parts, 9)

    status_code = _at(parts, 10)
    row["status_code"] = status_code
    row["status"] = _ORDER_STATUS_LABELS.get(status_code, status_code or "—")
    row["is_active"] = status_code in _ORDER_ACTIVE_STATUS_CODES

    row["product"] = _at(parts, 15)

    # 2026-08-25 實測抓到的缺口：委託日期/時間欄位（11/12）官方文件本來就有，
    # docstring 也早就寫了，但這裡從來沒有把它們解析出來，導致「歷史委託」
    # 畫面上完全看不到任何一筆單是什麼時候下的。這裡直接用原始數字組字串顯示
    # （不透過 pd.Timestamp(...).timestamp() 之類的時區換算——這只是純顯示用途，
    # 沒有要拿去做跨檔案的時間戳比對，直接組字串最安全，不會踩到 naive 時間戳
    # 被當成 UTC 的那個老問題）。
    order_date = _at(parts, 11)
    order_time = _at(parts, 12)
    if len(order_date) == 8 and len(order_time) >= 6:
        row["time"] = (
            f"{order_date[:4]}-{order_date[4:6]}-{order_date[6:8]} "
            f"{order_time[:2]}:{order_time[2:4]}:{order_time[4:6]}"
        )
    else:
        row["time"] = ""

    buy_sell = _at(parts, 22)
    row["direction"] = _direction_label(_normalize_direction(buy_sell))
    row["direction_key"] = _normalize_direction(buy_sell)

    row["new_close_flag"] = _at(parts, 33)  # Y當沖 N新倉 O平倉 A自動

    row["orig_qty"] = _at(parts, 30)
    row["filled_qty"] = _at(parts, 31)
    row["remaining_qty"] = _at(parts, 32)
    row["qty"] = row["orig_qty"] or row["filled_qty"]

    price = _at(parts, 27)
    avg_fill_price = _at(parts, 43)
    try:
        if avg_fill_price and float(avg_fill_price) > 0:
            price = avg_fill_price
    except ValueError:
        pass
    row["price"] = price
    return row


def parse_order_report_raw(raw: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in str(raw or "").splitlines():
        if line.strip():
            rows.append(parse_order_report_line(line))
    return rows


def _parse_number(value: str) -> float | None:
    text = str(value or "").strip().replace(",", "")
    if not text or text in {"-", "—"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_future_rights_record(record: str) -> dict[str, Any]:
    """解析 GetFutureRights / OnFutureRights 單筆 CSV（41 欄）。"""
    tokens = [t.strip() for t in str(record).split(",")]
    out: dict[str, Any] = {"raw": record.strip()}
    for idx, field in enumerate(_FUTURE_RIGHTS_FIELDS):
        if idx >= len(tokens):
            break
        raw_val = tokens[idx]
        if field in {
            "option_combo_margin_flag",
            "currency",
            "risk_indicator",
            "login_id",
            "account_no",
        }:
            out[field] = raw_val
        else:
            out[field] = _parse_number(raw_val)
    return out


def parse_future_rights_raw(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if not text:
        return {}
    records = [r for r in text.rstrip("#").split("#") if r.strip()]
    if not records:
        records = [text]
    parsed = parse_future_rights_record(records[-1])
    labels = {
        "account_balance": "帳戶餘額",
        "floating_pl": "浮動損益",
        "equity": "權益數",
        "available_balance": "可用餘額",
        "maintenance_margin": "維持保證金",
        "initial_margin": "原始保證金",
        "intraday_unrealized": "盤中未實現",
        "futures_close_pl": "期貨平倉損益",
        "risk_indicator": "風險指標",
        "maintenance_rate": "維持率",
    }
    parsed["labels"] = labels
    return parsed


def _parse_kline_datetime(date_part: str, time_part: str | None = None) -> int | None:
    # 2026-08-24 實測抓到的 bug：這裡原本直接用 pd.to_datetime(...).timestamp()，
    # pandas 對 naive（無時區）時間戳一律當 UTC 處理，但這些欄位其實是台灣本地
    # 時間，導致解析出來的 K 棒時間標籤比真實時間晚 8 小時。改用
    # timeutil.to_unix_seconds() 明確標記成 Asia/Taipei 再轉換。
    date_part = date_part.strip()
    # 群益新版分 K 常見：同一欄「YYYY/MM/DD HH:MM」或「YYYY/MM/DD HH:MM:SS」
    if time_part is None and re.match(
        r"^\d{4}[/-]\d{1,2}[/-]\d{1,2}\s+\d{1,2}:\d{2}", date_part
    ):
        for fmt in (
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
        ):
            try:
                return to_unix_seconds(pd.to_datetime(date_part, format=fmt))
            except ValueError:
                continue
        try:
            return to_unix_seconds(pd.to_datetime(date_part))
        except (ValueError, TypeError):
            return None

    if time_part:
        time_part = time_part.strip().replace(":", "")
        if len(time_part) == 4:
            time_part = time_part + "00"
        if len(time_part) == 6:
            for fmt in ("%Y/%m/%d %H%M%S", "%Y-%m-%d %H%M%S", "%Y%m%d %H%M%S"):
                try:
                    return to_unix_seconds(pd.to_datetime(f"{date_part} {time_part}", format=fmt))
                except ValueError:
                    continue
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%Y%m%d"):
        try:
            return to_unix_seconds(pd.to_datetime(date_part, format=fmt))
        except ValueError:
            continue
    return None


def parse_kline_row(raw: str) -> dict[str, Any] | None:
    """
    群益 OnNotifyKLineData 新版格式：
    - 日線：年/月/日, 開, 高, 低, 收, 量
    - 分線：時間可能獨立成欄，或與日期同一欄「YYYY/MM/DD HH:MM」
    """
    text = str(raw or "").strip()
    if not text:
        return None
    parts = [p.strip() for p in text.split(",") if p.strip() != ""]
    if len(parts) < 6:
        return None

    ts: int | None = None
    o = h = l = c = v = None

    if re.match(r"^\d{4}[/-]\d", parts[0]):
        if len(parts) >= 7 and re.match(r"^\d{1,2}:\d{2}", parts[1]):
            ts = _parse_kline_datetime(parts[0], parts[1])
            nums = parts[2:6]
            vol_idx = 6
        elif len(parts) >= 7 and parts[1].isdigit() and len(parts[1]) in {4, 6}:
            ts = _parse_kline_datetime(parts[0], parts[1])
            nums = parts[2:6]
            vol_idx = 6
        else:
            # 含「YYYY/MM/DD HH:MM, o,h,l,c,v」(6 欄) 或純日線 6 欄
            ts = _parse_kline_datetime(parts[0])
            nums = parts[1:5]
            vol_idx = 5
        if ts is None:
            return None
        try:
            o, h, l, c = (float(x) for x in nums[:4])
            v = int(float(parts[vol_idx]))
        except (ValueError, IndexError):
            return None
    else:
        try:
            o, h, l, c = (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))
            v = int(float(parts[4]))
            ts = int(pd.Timestamp.now().timestamp())
        except (ValueError, IndexError):
            return None

    return {
        "time": ts,
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": v,
        "raw": text,
    }


# 2026-10-07：GetStopLossReport / OnStopLossReport 查詢智慧單（含 STP/MIT/MST/OCO）
# 回報字串解析。欄位順序照官方文件《8.下單-國內期選智慧單》逐一核對——不是憑印象猜的，
# 是直接解析 docx 裡的 Word 表格結構（欄位名稱欄 vs 說明欄分開讀，不是把整份文件拉平成
# 一行行段落再用肉眼對位置，那樣容易因為表格跨行而對錯）。
# STP/MIT/MST 跟 OCO 是兩份不同的表格，前 33 個欄位（索引 0~32）共用，第 33 個欄位開始
# 分岔：OCO 多了「二擇一」專屬的六個欄位，所以同樣叫「長效單序號」的欄位，
# STP/MIT/MST 在索引 39，OCO 在索引 46。目前還沒有真實的群益回報可以核對這份對照表，
# 只確認了文件本身欄位名稱與順序一致；用之前（尤其是長效單相關欄位）务必先拿真實委託
# 的查詢結果核對一次。
_STOP_LOSS_REPORT_COMMON_FIELDS = (
    "smart_key",  # 0 智慧單號
    "market",  # 1 市場別
    "exchange_code",  # 2 交易所代碼
    "branch_code",  # 3 分公司代碼
    "account",  # 4 帳號
    "sub_account",  # 5 子帳
    "order_no",  # 6 委託書號
    "seq_no",  # 7 序號（13碼）
    "orig_seq_no",  # 8 原始序號（13碼）
    "strategy_kind",  # 9 策略別：3 OCO / 5 STP / 8 MIT / 9 MST
    "order_status",  # 10 智慧單委託狀態
    "trade_date",  # 11 交易日
    "order_time",  # 12 下單時間（建立智慧單時間）
    "product_code",  # 13 商品代碼（第一腳）
    "contract_month",  # 14 商品契約月份（第一腳）
    "strike_price",  # 15 履約價格
    "direction",  # 16 買賣別 B/S
    "order_price_kind",  # 17 委託價格別
    "order_price_type",  # 18 委託價格類別
    "trade_type",  # 19 委託時效 ROD/IOC/FOK
    "order_price",  # 20 委託價格
    "order_qty",  # 21 委託數量
    "trigger_price",  # 22 觸發價格
    "trigger_time",  # 23 觸發時間
    "message",  # 24 訊息
    "market_order_status",  # 25 市場委託狀態
    "updated_at",  # 26 智慧單異動時間
    "reserved_flag",  # 27 預約單註記
    "reserved_seq_no",  # 28 預約單序號
    "trade_date2",  # 29 交易日（第二個）
    "leg1_order_product_code",  # 30 第一腳下單商品代碼
    "leg2_order_product_code",  # 31 第二腳下單商品代碼
    "leg2_contract_month",  # 32 第二腳商品契約月份
)

_STOP_LOSS_REPORT_STP_TAIL_FIELDS = (
    "leg2_strike_price",  # 33 第二腳履約價格
    "new_close",  # 34 新平倉
    "is_day_trade",  # 35 是否為當沖
    "option_right",  # 36 買賣權
    "stop_on_first_trigger",  # 37 是否觸發即停止
    "long_action_flag",  # 38 長效單註記：0 非長效單 / 1 長效單
    "long_action_key",  # 39 長效單序號 ← 撤單用的長效單號
    "long_action_end_date",  # 40 長效單結束日期
    "long_action_condition",  # 41 長效單結束條件
    "long_action_order_qty",  # 42 長效單委託總量
    "long_action_filled_qty",  # 43 長效單成交總量
)

_STOP_LOSS_REPORT_OCO_TAIL_FIELDS = (
    "leg1_new_close",  # 33 第一腳倉位
    "is_day_trade",  # 34 是否為當沖
    "leg1_trigger_price",  # 35 第一腳觸發價
    "leg2_trigger_price",  # 36 第二腳觸發價
    "leg2_direction",  # 37 第二腳買賣別
    "leg2_order_price_kind",  # 38 第二腳委託價格別
    "leg2_order_price_type",  # 39 第二腳委託價格類別
    "leg2_order_price",  # 40 第二腳委託價
    "leg2_trade_type",  # 41 第二腳委託時效
    "leg2_new_close",  # 42 第二腳倉位
    "universal_message",  # 43 萬用訊息（狀態碼 999 時適用）
    "stop_on_first_trigger",  # 44 是否觸發即停止
    "long_action_flag",  # 45 長效單註記：0 非長效單 / 1 長效單
    "long_action_key",  # 46 長效單序號 ← 撤單用的長效單號
    "long_action_end_date",  # 47 長效單結束日期
    "long_action_condition",  # 48 長效單結束條件
    "long_action_order_qty",  # 49 長效單委託總量
    "long_action_filled_qty",  # 50 長效單成交總量
)

STOP_LOSS_STRATEGY_OCO = "3"


def parse_stop_loss_report_line(line: str) -> dict[str, Any]:
    """解析 GetStopLossReport 查詢回報的一行資料（不含 "M000,n" 開頭行與 "##" 結尾行）。"""
    text = line.strip()
    if not text:
        return {}
    parts = [p.strip() for p in text.split(",")]
    if len(parts) < len(_STOP_LOSS_REPORT_COMMON_FIELDS):
        return {"raw": text}

    row: dict[str, Any] = {"raw": text}
    for idx, key in enumerate(_STOP_LOSS_REPORT_COMMON_FIELDS):
        row[key] = _at(parts, idx)

    is_oco = row.get("strategy_kind") == STOP_LOSS_STRATEGY_OCO
    tail_fields = _STOP_LOSS_REPORT_OCO_TAIL_FIELDS if is_oco else _STOP_LOSS_REPORT_STP_TAIL_FIELDS
    offset = len(_STOP_LOSS_REPORT_COMMON_FIELDS)
    for idx, key in enumerate(tail_fields):
        row[key] = _at(parts, offset + idx)

    row["is_long_action"] = row.get("long_action_flag") == "1"
    return row


def parse_stop_loss_report_raw(raw: str) -> list[dict[str, Any]]:
    """
    解析多筆 GetStopLossReport 查詢回報（每個 OnStopLossReport 事件一行，依序串起來）。
    自動跳過開頭的 "M###,n"（成功/失敗旗標）與結尾的 "##..."（查詢結束標記），
    這兩種不是實際的委託資料列。
    """
    rows: list[dict[str, Any]] = []
    for line in str(raw or "").splitlines():
        text = line.strip()
        if not text:
            continue
        if re.match(r"^M\d{3},\d+$", text) or text.startswith("##"):
            continue
        rows.append(parse_stop_loss_report_line(text))
    return rows