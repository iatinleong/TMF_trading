"""
共用的時間戳轉換工具，全專案唯一一個「naive Taiwan 本地時間 → UNIX 秒數」的
實作，避免各檔案各自重複同一段轉換邏輯、漏改就出現時間標籤跑掉的問題（見
2026-08-21 api.py::_to_unix_seconds 的原始修復、以及 2026-08-24
kline_engine.py/capital_parse.py 補修同一個 bug 的教訓）。

pandas 的 Timestamp.timestamp() 對沒有時區資訊（naive）的時間戳，一律當成
UTC 處理，不是當地系統時區——這點跟 Python 內建 datetime.timestamp() 的行為
不一樣，很容易誤用。專案裡所有 K 棒/交易時間戳都是台灣本地時間（naive，沒帶
時區資訊），要先明確標記為 Asia/Taipei 再轉换，才能讓前端顯示、跟真實台灣
時間對得起來。
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import pandas as pd

TAIPEI_TZ = ZoneInfo("Asia/Taipei")


def to_unix_seconds(value: object) -> int:
    """把 naive（或已帶時區）的時間戳轉成正確的 UNIX 秒數。"""
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(TAIPEI_TZ)
    return int(timestamp.timestamp())
