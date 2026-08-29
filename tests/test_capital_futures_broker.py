from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from backend.broker import capital_futures
from backend.broker.capital_futures import CapitalFuturesBroker
from backend.kline_engine import get_store
from backend.timeutil import TAIPEI_TZ


@pytest.fixture(autouse=True)
def _isolate_audit_logs(tmp_path, monkeypatch):
    """
    2026-08-25：這個檔案裡好幾個測試會實際呼叫 send_future_order/
    _parse_open_interest 等真的會落地寫稽核 log 的函式，原本沒有隔離路徑，
    每次跑測試都會把假資料寫進專案真實的 data/order_audit.log、
    data/position_audit.log——這兩個檔案是設計來給事後查真實事故用的，
    混進測試假資料會讓人搞不清楚哪些是真的。全部測試預設隔離到 tmp_path，
    不用每個測試各自手動 monkeypatch。
    """
    monkeypatch.setattr(
        capital_futures, "_order_audit_log_path", lambda: tmp_path / "order_audit.log"
    )
    monkeypatch.setattr(
        capital_futures, "_position_audit_log_path", lambda: tmp_path / "position_audit.log"
    )


def test_parse_open_interest_appends_position_audit_log(tmp_path):
    """
    2026-08-25：使用者回報「開單成功後，持倉畫面卻一直是空的」，但事後只能查
    SKCOM 自己的 Order.log——那份 log 會被重置，還混進了後續診斷連線的內容，
    沒辦法拿來當乾淨證據回頭查。這裡驗證每次 _parse_open_interest 執行都會
    落地存一筆稽核紀錄，下次再發生同樣狀況能有乾淨的歷史可查。
    """
    audit_path = tmp_path / "position_audit.log"

    broker = CapitalFuturesBroker()
    broker._parse_open_interest("TF,F0200006921941,TMFR1,B,2,0,21500")
    broker._parse_open_interest("")

    lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "count=1" in lines[0]
    assert "TMFR1/longx2" in lines[0]
    assert "count=0" in lines[1]


def test_apply_stock_quote_does_not_double_count_cumulative_volume():
    """
    2026-08-25 實測抓到的 bug：SKSTOCK.nTQty 官方文件（readme_doc）標示為「總量」
    ——今日累計成交量，不是這次輪詢新增的量。_apply_stock_quote()（request_stocks()
    輪詢路徑）原本把它當成單次增量傳給 _on_tick_for_kline，每次輪詢（大約每
    10~30 秒一次）都會把同一個、且持續變大的累計總量重複疊加進當前這根K棒的
    volume，短短一小時內就能疊到幾百萬，完全脫離真實成交量。真正的逐筆成交量
    應該只由 OnNotifyTicksLONG（官方文件標示 nQty 為「成交量」，這次是正確的
    單筆增量）累加。
    """
    broker = _ready_broker()
    broker.live.subscribed_product = "TESTVOLBUG"

    fixed_taipei = datetime(2026, 8, 25, 10, 0, 0, tzinfo=TAIPEI_TZ)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_taipei if tz is not None else fixed_taipei.replace(tzinfo=None)

    stock = SimpleNamespace(
        bstrStockNo="TESTVOLBUG", nClose=2000000, nBid=1999900, nAsk=2000100, nTQty=50000
    )

    with patch("backend.broker.capital_futures.datetime", _FixedDatetime):
        # 模擬輪詢好幾次，nTQty（今日累計量）持續變大
        for cumulative_qty in (50000, 50010, 50025, 50040):
            stock.nTQty = cumulative_qty
            broker._apply_stock_quote(stock)

    bar = get_store("TESTVOLBUG").latest_bar()
    assert bar is not None
    assert bar["volume"] == 0


def _ready_broker() -> CapitalFuturesBroker:
    broker = CapitalFuturesBroker()
    broker._logged_in = True
    broker._order_initialized = True
    broker.user_id = "96571013"
    broker.order = MagicMock()
    return broker


def test_send_future_order_success_when_code_is_second_return_value():
    """
    2026-08-25 實測抓到的重大 bug：用 comtypes 對 SKCOM.dll 型別庫核對過，
    SendFutureOrderCLR 官方 COMMETHOD 定義是 (['out'] bstrMessage,
    ['out','retval'] retCode)——跟 STP/MIT 智慧單一樣是 message 在前、
    code 在後，不是原本假設的 code 在前。真實案例：VM 上一筆真的成功的市價
    委託，SendFutureOrderCLR 回傳 ("委託書:d4-977 買進...", "0")，原本用
    code, message = ... 解包會把委託書文字誤判成 code，code==0 恆為 False，
    導致每一筆真正成功的一般委託都被誤判失敗。
    """
    broker = _ready_broker()
    broker.order.SendFutureOrderCLR.return_value = ("委託書:d4-977 買進FITM 202609 新 E 1口 委價市價", 0)

    result = broker.send_future_order(
        account="F0200006921941", product_code="TM2609", side=0, qty=1
    )

    assert result.success is True
    assert result.code == 0


def test_send_future_order_failure_when_code_is_nonzero():
    broker = _ready_broker()
    broker.order.SendFutureOrderCLR.return_value = ("[530] 委託單類別(ORDER TYPE)輸入錯誤", 530)

    result = broker.send_future_order(
        account="F0200006921941", product_code="TM2609", side=0, qty=1
    )

    assert result.success is False
    assert result.code == 530


def test_cancel_order_by_seqno_uses_message_first_order():
    broker = _ready_broker()
    broker.order.CancelOrderBySeqNo.return_value = ("刪單成功", 0)

    result = broker.cancel_order_by_seqno(account="F0200006921941", seq_no="1234567890123")

    assert result.success is True


def test_correct_price_by_seqno_uses_message_first_order():
    broker = _ready_broker()
    broker.order.CorrectPriceBySeqNo.return_value = ("改價成功", 0)

    result = broker.correct_price_by_seqno(
        account="F0200006921941", seq_no="1234567890123", price="20000"
    )

    assert result.success is True


def test_send_future_oco_order_success_builds_both_legs():
    """
    2026-08-27 實盤事故根因：原本分開送 STP + MIT 兩張獨立平倉智慧單，第二張
    被券商拒絕（[999] 勾選平倉而留倉部位不足），因為兩張各自向券商聲請同一口
    的平倉額度。群益官方範例（TFStrategyOrder.py::buttonSendFutureOCOOrderV1_Click）
    有專門的 SendFutureOCOOrderV1，一次委託把兩支腿（bstrTrigger/bstrPrice 跟
    bstrTrigger2/bstrPrice2）都帶上，讓券商當成一組配對好的二擇一單處理，不是
    兩張互相搶額度的獨立委託。這裡驗證 wrapper 把兩支腿的參數都正確放進
    FUTUREORDER 物件、且跟其他智慧單一樣是 message 在前、code 在後。
    """
    broker = _ready_broker()
    broker.order.SendFutureOCOOrderV1.return_value = ("20260827,二擇一委託已送出 條件單號：26999999,x1234,26999999,1688200000001", 0)

    result = broker.send_future_oco_order(
        account="F0200006921941",
        stock_no="TMF",
        settlement_month="202609",
        qty=1,
        side=1,  # 停損腿：賣出
        trigger_price="45850",
        price="P",
        side2=1,  # 停利腿：賣出
        trigger_price2="46650",
        price2="P",
        new_close=1,
    )

    assert result.success is True
    sent_order = broker.order.SendFutureOCOOrderV1.call_args[0][2]
    assert sent_order.bstrTrigger == "45850"
    assert sent_order.bstrPrice == "P"
    assert sent_order.sBuySell == 1
    assert sent_order.bstrTrigger2 == "46650"
    assert sent_order.bstrPrice2 == "P"
    assert sent_order.sBuySell2 == 1
    assert sent_order.nQty == 1
    assert sent_order.bstrStockNo == "TMF"
    assert sent_order.bstrSettlementMonth == "202609"
    assert sent_order.sNewClose == 1


def test_send_future_oco_order_failure_when_code_is_nonzero():
    broker = _ready_broker()
    broker.order.SendFutureOCOOrderV1.return_value = ("[999] 額度不足", 999)

    result = broker.send_future_oco_order(
        account="F0200006921941",
        stock_no="TMF",
        settlement_month="202609",
        qty=1,
        side=1,
        trigger_price="45850",
        price="P",
        side2=1,
        trigger_price2="46650",
        price2="P",
    )

    assert result.success is False
    assert result.code == 999


def test_on_tick_for_kline_polling_path_uses_taipei_tz_not_system_clock():
    """
    2026-08-25 實測抓到的 bug：request_stocks() 輪詢路徑用 n_date=0 呼叫
    _on_tick_for_kline 時，原本退回 pd.Timestamp.now()（讀作業系統本地時區的
    naive 時間戳）。在系統時區不是台灣的機器（例如全新 GCP VM，預設常是 UTC）
    上，這會讓輪詢推送的 K 棒被標到錯誤的日期/時段。改用 datetime.now(TAIPEI_TZ)
    後，不管作業系統本身的時區設定為何，都應該得到正確的台灣當地時間。
    """
    broker = CapitalFuturesBroker()
    broker.live.subscribed_product = "TM2609TESTTZ"

    # 模擬系統時區是 UTC 的機器：固定 UTC 時刻 2026-08-25 02:00:00，
    # 對應台灣時間應該是同一天 10:00:00（日盤時段內）。
    fixed_utc = datetime(2026, 8, 25, 2, 0, 0, tzinfo=timezone.utc)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                # 模擬有 bug 的寫法：naive now() 直接回傳系統本地讀數
                # （這裡假設系統時區恰好是 UTC）。
                return fixed_utc.replace(tzinfo=None)
            return fixed_utc.astimezone(tz)

    with patch("backend.broker.capital_futures.datetime", _FixedDatetime):
        broker._on_tick_for_kline(21500.0, 1, 0, 0)

    bar = get_store("TM2609TESTTZ").latest_bar()
    assert bar is not None

    bar_time = datetime.fromtimestamp(bar["time"], tz=TAIPEI_TZ)
    # 修好之後應該落在台灣時間 2026-08-25 的日盤時段（08:45~13:45）。
    # 如果退回去用系統 naive now()，會被誤判成 2026-08-24 的夜盤，日期就錯了。
    assert bar_time.strftime("%Y-%m-%d") == "2026-08-25"
    assert bar_time.hour in (8, 9, 10, 11, 12, 13)


def test_parse_open_interest_does_not_commit_before_terminator():
    """
    2026-08-30 實盤事故：群益 OnOpenInterest 對「一次查詢」的回應，實際上是
    分成好幾個獨立的事件呼叫送過來的——一筆真正的部位資料一次呼叫，接著
    幾毫秒後再一次呼叫送「##,,,,...」這個查詢結束終止符（用真實
    position_audit.log 逐筆核對過，永遠成對出現）。舊版程式碼每次呼叫都
    無條件覆寫 self.live.positions，導致終止符那次呼叫（解析不出任何部位）
    把剛剛才寫進去、真正存在的部位資料整個蓋掉，變成 self.live.positions
    有 99% 的時間都是空的——這也是為什麼獨立連線去查詢時，即使券商查詢
    狀態明確回報成功，看到的還是空倉。

    這裡驗證：只送一筆資料列、還沒收到終止符之前，不該直接寫進
    self.live.positions（要先緩衝、等終止符才正式提交）。
    """
    broker = CapitalFuturesBroker()
    broker._parse_open_interest("TF,F0200006921941,TMFR1,B,2,0,21500")
    assert broker.live.positions == []


def test_parse_open_interest_commits_buffered_rows_on_terminator():
    """資料列 + 終止符（真實 SDK 的實際兩段式回應）送完之後，才正式提交進 self.live.positions。"""
    broker = CapitalFuturesBroker()
    broker._parse_open_interest("TF,F0200006921941,TMFR1,B,2,0,21500")
    broker._parse_open_interest("##,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,")

    assert len(broker.live.positions) == 1
    assert broker.live.positions[0]["product"] == "TMFR1"


def test_parse_open_interest_clears_on_empty_snapshot():
    # 2026-08-21 修正：帳戶回到空手時，OnOpenInterest 推送的空快照也要能清空
    # self.live.positions，不然畫面上的持倉表格會卡在最後一筆已經不存在的舊資料。
    # 2026-08-30：改用真實的兩段式回應（查無資料 + 終止符）模擬空倉查詢。
    broker = CapitalFuturesBroker()
    broker._parse_open_interest("TF,F0200006921941,TMFR1,B,2,0,21500")
    broker._parse_open_interest("##,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,")
    assert broker.live.positions

    broker._parse_open_interest("001,查無資料,F0200006921941")
    broker._parse_open_interest("##,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,")

    assert broker.live.positions == []


def test_parse_open_interest_bare_empty_string_still_commits_empty():
    """空字串（非真實終止符格式，但當年舊測試假設過的邊界情況）也要能觸發提交清空，向下相容。"""
    broker = CapitalFuturesBroker()
    broker._parse_open_interest("TF,F0200006921941,TMFR1,B,2,0,21500")
    broker._parse_open_interest("##,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,")
    assert broker.live.positions

    broker._parse_open_interest("")

    assert broker.live.positions == []


def test_position_audit_log_includes_account_tag(tmp_path):
    """
    2026-08-26：多帳號規劃第一步（見
    docs/superpowers/plans/2026-08-26-per-account-worker-process.md）——
    position_audit.log 原本沒有標記是哪個帳號的持倉，一個帳號一個 worker
    行程之後，即使各自寫各自的行程還是同一個檔名慣例，加上帳號標記才好
    事後對照是哪個帳號的紀錄。
    """
    audit_path = tmp_path / "position_audit.log"

    broker = CapitalFuturesBroker()
    broker.user_id = "test_user_123"
    broker._parse_open_interest("TF,F0200006921941,TMFR1,B,2,0,21500")

    content = audit_path.read_text(encoding="utf-8")
    assert "account=test_user_123" in content
