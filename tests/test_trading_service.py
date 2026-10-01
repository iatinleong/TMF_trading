from datetime import date, timedelta

from backend.trading_service import compute_current_tmf_code, days_since_tmf_rollover, third_wednesday


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


def test_days_since_tmf_rollover_on_rollover_day_is_zero():
    # 換月當天（上一口結算隔天）本身：目前近月合約剛開始算近月，安全回溯天數=0。
    rollover_day = third_wednesday(2026, 9) + timedelta(days=1)
    assert days_since_tmf_rollover(rollover_day) == 0


def test_days_since_tmf_rollover_counts_up_within_same_contract():
    # 9/30 距離 9/16 結算（換月隔天 9/17 開始當近月）已經 13 天。
    assert days_since_tmf_rollover(date(2026, 9, 30)) == 13


def test_days_since_tmf_rollover_before_this_months_settlement():
    # 9/10 在9月結算(9/16)之前：目前近月合約是TM2609，是從8月結算隔天開始算近月。
    rollover_start = third_wednesday(2026, 8) + timedelta(days=1)
    expected = (date(2026, 9, 10) - rollover_start).days
    assert days_since_tmf_rollover(date(2026, 9, 10)) == expected


def test_days_since_tmf_rollover_never_negative():
    # 結算日當天（還沒換月，仍是舊合約近月的最後一天）：至少要是非負數。
    assert days_since_tmf_rollover(third_wednesday(2026, 9)) >= 0


def test_days_since_tmf_rollover_rolls_across_year_boundary():
    rollover_day = third_wednesday(2026, 12) + timedelta(days=1)
    assert days_since_tmf_rollover(rollover_day) == 0
    ten_days_later = rollover_day + timedelta(days=10)
    assert days_since_tmf_rollover(ten_days_later) == 10
