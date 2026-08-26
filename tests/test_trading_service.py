from datetime import date, timedelta

from backend.trading_service import compute_current_tmf_code, third_wednesday


def test_third_wednesday_august_2026():
    # 實際驗證過：2026-08 的結算日是 08-19（週三），TM2608 在 08-19 之後就沒有報價了。
    assert third_wednesday(2026, 8) == date(2026, 8, 19)
    assert third_wednesday(2026, 8).weekday() == 2  # Wednesday


def test_compute_current_tmf_code_before_settlement():
    assert compute_current_tmf_code(date(2026, 8, 19)) == "TM2608"
    assert compute_current_tmf_code(date(2026, 8, 1)) == "TM2608"


def test_compute_current_tmf_code_after_settlement():
    # 2026-08-21 實測：TM2608 報價是 null，TM2609 才有正常報價，跟這個函式算出來的一致。
    assert compute_current_tmf_code(date(2026, 8, 21)) == "TM2609"
    assert compute_current_tmf_code(date(2026, 8, 20)) == "TM2609"


def test_compute_current_tmf_code_rolls_across_year_boundary():
    # 12月結算後應該跳到隔年1月，不是月份變13。
    after_settlement = third_wednesday(2026, 12) + timedelta(days=1)
    assert compute_current_tmf_code(after_settlement) == "TM2701"
