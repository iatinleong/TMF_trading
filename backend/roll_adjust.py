"""換月價差調整（back-adjust）。

TMFR1 是連續月代碼：每月第三個星期三結算後，近月合約換成下一個月，兩個合約的價位不同（基差）。
直接接在一起，價格會在換月處垂直跳開，均線、交叉與停損會被扭曲。

做法：在每個換月點，Δ = 新合約第一根開盤 − 舊合約最後一根收盤（市場在 13:45 到 15:00 休市，
這個差值主要是基差），然後把換月點之前的所有價格整體加上 Δ，讓序列在換月處接平。
成交量不調整；換月之後的價格保持不變，所以同一合約內的點數損益不受影響。
"""

from __future__ import annotations

import calendar

import pandas as pd

PRICE_COLUMNS = ("open", "high", "low", "close")
_DAY_SESSION_START = pd.Timedelta(hours=8, minutes=45)
_DAY_SESSION_END = pd.Timedelta(hours=13, minutes=45)
_NIGHT_SESSION_START = pd.Timedelta(hours=15)


def third_wednesdays(years: range | list[int]) -> list[pd.Timestamp]:
    """每月第三個星期三（台指期結算日）。"""
    days: list[pd.Timestamp] = []
    for year in years:
        for month in range(1, 13):
            weeks = calendar.monthcalendar(year, month)
            wednesdays = [week[2] for week in weeks if week[2]]
            days.append(pd.Timestamp(year, month, wednesdays[2]))
    return days


def find_roll_points(bars: pd.DataFrame, settlement_days: list[pd.Timestamp]) -> list[tuple[pd.Timestamp, float]]:
    """找出資料中的換月點，回傳 [(新合約第一根 K 棒時間, Δ)]。

    只有結算日的日盤最後一根與夜盤第一根都存在時才算換月點；缺資料的結算日會略過（不猜）。
    """
    if bars.empty:
        return []
    index = bars.index
    day_norm = index.normalize()
    tod = index - day_norm

    rolls: list[tuple[pd.Timestamp, float]] = []
    for settlement in settlement_days:
        on_day = day_norm == settlement
        day_mask = on_day & (tod >= _DAY_SESSION_START) & (tod <= _DAY_SESSION_END)
        night_mask = on_day & (tod >= _NIGHT_SESSION_START)
        if not day_mask.any() or not night_mask.any():
            continue
        old_last = bars[day_mask].iloc[-1]
        new_first = bars[night_mask].iloc[0]
        delta = float(new_first["open"]) - float(old_last["close"])
        rolls.append((pd.Timestamp(bars[night_mask].index[0]), delta))
    return rolls


def adjust_roll_prices(bars: pd.DataFrame) -> pd.DataFrame:
    """依資料涵蓋的年份找出所有結算日換月點，並做價差調整。資料為空時原樣回傳。"""
    if bars.empty:
        return bars
    years = range(bars.index.min().year, bars.index.max().year + 1)
    return back_adjust(bars, find_roll_points(bars, third_wednesdays(years)))


def back_adjust(bars: pd.DataFrame, rolls: list[tuple[pd.Timestamp, float]]) -> pd.DataFrame:
    """把每個換月點之前的價格加上該點的 Δ（由新到舊累加，等同於從最近一次換月往回調整）。"""
    adjusted = bars.copy()
    if not rolls:
        return adjusted
    for roll_time, delta in sorted(rolls, key=lambda item: item[0], reverse=True):
        before = adjusted.index < roll_time
        for column in PRICE_COLUMNS:
            adjusted.loc[before, column] = adjusted.loc[before, column] + delta
    return adjusted
