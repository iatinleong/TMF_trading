"""GetStopLossReport / OnStopLossReport 查詢回報解析測試。

這裡的範例字串，是照官方文件《8.下單-國內期選智慧單》裡 Word 表格的欄位順序
一個一個欄位組出來的（不是真實群益回報，群益目前還沒有真的回過一筆含長效單的
查詢結果給我們核對過）。驗證目標是「解析器有照文件的欄位順序讀」，不是「這份
欄位順序本身保證跟真實回報一致」——後者要等真實測試單的查詢結果才能確認。
"""

from backend.capital_parse import parse_stop_loss_report_line, parse_stop_loss_report_raw


def _stp_fields(*, long_action_flag="1", long_action_key="LA000123"):
    # 索引 0~32 為 STP/OCO 共用欄位，33~43 為 STP/MIT/MST 專屬欄位（共 44 欄）。
    common = [
        "27169464",  # 0 智慧單號
        "TF",  # 1 市場別
        "TAIFEX",  # 2 交易所代碼
        "",  # 3 分公司代碼
        "F0200006921941",  # 4 帳號
        "",  # 5 子帳
        "u5490",  # 6 委託書號
        "1688300002076",  # 7 序號
        "",  # 8 原始序號
        "5",  # 9 策略別：5=STP
        "34",  # 10 智慧單委託狀態：洗價中
        "20261006",  # 11 交易日
        "220008",  # 12 下單時間
        "TMF",  # 13 商品代碼
        "202610",  # 14 商品契約月份
        "0",  # 15 履約價格
        "S",  # 16 買賣別
        "7",  # 17 委託價格別
        "2",  # 18 委託價格類別
        "0",  # 19 委託時效
        "49870",  # 20 委託價格
        "1",  # 21 委託數量
        "49870",  # 22 觸發價格
        "",  # 23 觸發時間
        "智慧單回報：[27169464][TMF]洗價中",  # 24 訊息
        "",  # 25 市場委託狀態
        "20261006220008",  # 26 智慧單異動時間
        "0",  # 27 預約單註記
        "",  # 28 預約單序號
        "20261006",  # 29 交易日(第二個)
        "TMF",  # 30 第一腳下單商品代碼
        "",  # 31 第二腳下單商品代碼
        "",  # 32 第二腳商品契約月份
    ]
    tail = [
        "",  # 33 第二腳履約價格
        "1",  # 34 新平倉：1=平倉
        "",  # 35 是否為當沖
        "N",  # 36 買賣權
        "1",  # 37 是否觸發即停止
        long_action_flag,  # 38 長效單註記
        long_action_key,  # 39 長效單序號
        "20261021",  # 40 長效單結束日期
        "1",  # 41 長效單結束條件
        "1",  # 42 長效單委託總量
        "0",  # 43 長效單成交總量
    ]
    return common + tail


def _oco_fields(*, long_action_flag="1", long_action_key="LA000456"):
    common = _stp_fields()[:33]
    common[9] = "3"  # 策略別：3=OCO
    tail = [
        "1",  # 33 第一腳倉位
        "",  # 34 是否為當沖
        "50470",  # 35 第一腳觸發價
        "49870",  # 36 第二腳觸發價
        "S",  # 37 第二腳買賣別
        "7",  # 38 第二腳委託價格別
        "2",  # 39 第二腳委託價格類別
        "49870",  # 40 第二腳委託價
        "0",  # 41 第二腳委託時效
        "1",  # 42 第二腳倉位
        "",  # 43 萬用訊息
        "1",  # 44 是否觸發即停止
        long_action_flag,  # 45 長效單註記
        long_action_key,  # 46 長效單序號
        "20261021",  # 47 長效單結束日期
        "1",  # 48 長效單結束條件
        "1",  # 49 長效單委託總量
        "0",  # 50 長效單成交總量
    ]
    return common + tail


def test_parse_stp_line_extracts_long_action_key_at_documented_position():
    line = ",".join(_stp_fields())
    row = parse_stop_loss_report_line(line)

    assert row["smart_key"] == "27169464"
    assert row["strategy_kind"] == "5"
    assert row["product_code"] == "TMF"
    assert row["is_long_action"] is True
    assert row["long_action_key"] == "LA000123"
    assert row["long_action_end_date"] == "20261021"


def test_parse_oco_line_extracts_long_action_key_at_oco_specific_position():
    line = ",".join(_oco_fields())
    row = parse_stop_loss_report_line(line)

    assert row["smart_key"] == "27169464"
    assert row["strategy_kind"] == "3"
    assert row["is_long_action"] is True
    assert row["long_action_key"] == "LA000456"
    # OCO 專屬欄位，確認沒有被 STP 的欄位對照表誤判
    assert row["leg2_direction"] == "S"
    assert row["leg2_order_price"] == "49870"


def test_parse_line_non_long_action_order_has_flag_false():
    line = ",".join(_stp_fields(long_action_flag="0", long_action_key="0"))
    row = parse_stop_loss_report_line(line)

    assert row["is_long_action"] is False
    assert row["long_action_flag"] == "0"


def test_parse_raw_skips_header_and_terminator_and_handles_multiple_rows():
    stp_line = ",".join(_stp_fields())
    oco_line = ",".join(_oco_fields())
    raw = "\n".join(
        [
            "M000,2",  # 查詢成功，共 2 筆
            stp_line,
            oco_line,
            "##,,,,",  # 查詢結束標記
        ]
    )

    rows = parse_stop_loss_report_raw(raw)

    assert len(rows) == 2
    assert rows[0]["strategy_kind"] == "5"
    assert rows[0]["long_action_key"] == "LA000123"
    assert rows[1]["strategy_kind"] == "3"
    assert rows[1]["long_action_key"] == "LA000456"


def test_parse_line_too_short_falls_back_to_raw_only():
    row = parse_stop_loss_report_line("27169464,TF,TAIFEX")
    assert row == {"raw": "27169464,TF,TAIFEX"}
