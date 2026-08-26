from backend.timeutil import to_unix_seconds


def test_naive_timestamp_treated_as_taipei_not_utc():
    # 2026-08-24 實測抓到的真實 bug：pandas 對 naive 時間戳一律當 UTC 處理，
    # 但我們存的都是台灣本地時間，之前 kline_engine.py/capital_parse.py 各自
    # 直接用 pd.Timestamp(...).timestamp()，導致即時K棒標籤比真實時間晚8小時
    # （使用者看到「最新K棒」變成隔天，誤以為缺K棒）。
    epoch = to_unix_seconds("2025-01-07 23:00:00")
    # 直接用「當成 UTC」的錯誤算法算出來的結果，拿來確認我們沒有走回頭路。
    wrong_epoch_if_treated_as_utc = int(
        __import__("pandas").Timestamp("2025-01-07 23:00:00").timestamp()
    )
    assert epoch != wrong_epoch_if_treated_as_utc
    assert wrong_epoch_if_treated_as_utc - epoch == 8 * 3600


def test_round_trips_to_correct_taipei_wall_clock():
    import datetime
    import zoneinfo

    epoch = to_unix_seconds("2025-01-07 23:00:00")
    back = datetime.datetime.fromtimestamp(epoch, tz=zoneinfo.ZoneInfo("Asia/Taipei"))
    assert back.strftime("%Y-%m-%d %H:%M:%S") == "2025-01-07 23:00:00"


def test_already_tz_aware_timestamp_is_left_alone():
    import pandas as pd

    aware = pd.Timestamp("2025-01-07 23:00:00", tz="Asia/Taipei")
    assert to_unix_seconds(aware) == int(aware.timestamp())
