"""即時 60 分 K 線：群益 K 線 API 回補 + tick 合成（不含 CSV 暖機）。"""

from __future__ import annotations

import json
import logging
import re
from datetime import time
from pathlib import Path

import numpy as np
import pandas as pd

from .capital_parse import parse_kline_row
from .config import app_base_dir
from .timeutil import to_unix_seconds

logger = logging.getLogger(__name__)

DEFAULT_KLINE_LIMIT = 500
_BAR_LIMIT = DEFAULT_KLINE_LIMIT

# 2026-08-25 實測抓到的資料缺口：群益 RequestKLineAMByDate 回補似乎要整個交易
# 時段（日盤/夜盤）真正收盤後才會補齊那個時段的資料——時段進行中，就算是已經
# 收盤的那幾根，也完全查不到，只有這次連線後即時 tick 現場建出來的才有。這代表
# 後端如果在某個時段「進行中」重開（本專案這幾天因為除錯常常這樣），重開前已經
# 收盤、但那個時段還沒完全結束的 K 棒會永久拿不回來，直到該時段結束回補 API 才
# 會補齊。解法：每次確定一根 K 棒收盤（下一筆 tick 開出新的一根），就把目前完整
# 的 _bars 落地存檔，下次啟動時先讀回來，蓋掉這個視窗期。
_CACHE_DIR = app_base_dir(__file__) / "data" / "kline_cache"


def _cache_path(product_code: str, interval_minutes: int = 60) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", product_code.upper())
    suffix = "" if interval_minutes == 60 else f"_{interval_minutes}min"
    return _CACHE_DIR / f"{safe}{suffix}.json"


def bar_close_time_from_ts(ts: pd.Timestamp, minutes: int = 60) -> pd.Timestamp | None:
    """台指期交易時段內，將時間對齊到 `minutes` 分鐘寬的 K 棒收盤時間（預設60分鐘）。
    過濾週末與非交易時段：
    1. 日盤 (08:45 ~ 13:45): 週一(0) ~ 週五(4)
    2. 夜盤 (15:00 ~ 05:00): 週一(0) 15:00 到週六(5) 05:00 結束
    週六 05:00 以後、週日全天、週一 08:45 以前為休市時段。
    """
    ts = pd.Timestamp(ts)
    if ts.tzinfo is not None:
        ts = ts.tz_localize(None)

    weekday = ts.dayofweek  # 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun
    t = ts.time()

    is_day = (0 <= weekday <= 4) and (time(8, 45) <= t <= time(13, 45))
    is_night_evening = (0 <= weekday <= 4) and (t >= time(15, 0))  # 週一至週五 15:00 ~ 23:59
    is_night_morning = (1 <= weekday <= 5) and (t <= time(5, 0))   # 週二至週六 00:00 ~ 05:00 (屬於前一交易日夜盤)

    if not (is_day or is_night_evening or is_night_morning):
        return None

    trade_date = ts.normalize()
    if is_night_morning:
        trade_date = trade_date - pd.Timedelta(days=1)

    if is_day:
        session_start = trade_date + pd.Timedelta(hours=8, minutes=45)
    else:
        session_start = trade_date + pd.Timedelta(hours=15)

    elapsed = (ts - session_start) / pd.Timedelta(minutes=1)
    bar_no = int(np.ceil(max(float(elapsed), 0.0) / float(minutes)))
    if bar_no == 0:
        bar_no = 1
    return pd.Timestamp(session_start + pd.Timedelta(minutes=bar_no * minutes))


class LiveKlineStore:
    def __init__(self, product_code: str, interval_minutes: int = 60) -> None:
        self.product_code = product_code
        self.interval_minutes = interval_minutes
        self._bars: dict[int, dict] = {}
        self._load_cache()

    def _load_cache(self) -> None:
        path = _cache_path(self.product_code, self.interval_minutes)
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            self._bars = {int(key): bar for key, bar in raw.items()}
        except Exception:  # noqa: BLE001 - 快取壞掉不該讓啟動失敗，當作沒有快取
            logger.warning("讀取 K 線快取失敗（%s），略過", path, exc_info=True)

    def _persist(self) -> None:
        try:
            path = _cache_path(self.product_code, self.interval_minutes)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({str(key): bar for key, bar in self._bars.items()}),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception:  # noqa: BLE001 - 落地失敗不該影響即時交易主流程
            logger.warning("寫入 K 線快取失敗（%s）", self.product_code, exc_info=True)

    def _trim_bars(self) -> None:
        if len(self._bars) <= _BAR_LIMIT:
            return
        for key in sorted(self._bars.keys())[: len(self._bars) - _BAR_LIMIT]:
            del self._bars[key]

    def ingest_kline_row(self, raw: str) -> None:
        parsed = parse_kline_row(raw)
        if not parsed:
            return
        self._bars[int(parsed["time"])] = parsed
        self._trim_bars()

    def on_tick(self, price: float, volume: int, ts: pd.Timestamp | None = None) -> None:
        if price <= 0:
            return
        if ts is None or pd.isna(ts):
            ts = pd.Timestamp.now()
        close_time = bar_close_time_from_ts(ts, minutes=self.interval_minutes)
        if close_time is None:
            return
        # 2026-08-24 實測抓到的 bug：close_time 是 naive 台灣本地時間，pandas
        # 的 .timestamp() 對 naive 值一律當 UTC 處理，導致即時K棒的時間標籤
        # 比真實時間晚 8 小時（使用者看到「最新K棒」標成隔天，誤以為缺K棒）。
        key = to_unix_seconds(close_time)
        bar = self._bars.get(key)
        if bar is None:
            # 2026-08-25：開出新的一根，代表前一根（如果有）已經確定收盤，不會再
            # 有更新——這是唯一能確定某根 K 棒「定案」的時機點。這裡順手落地存檔，
            # 這樣後端重開後還讀得回這根，不用等群益 RequestKLineAMByDate 那個
            # 「整個交易時段收完盤才補齊」的回補（見檔頭常數註解）。
            if self._bars:
                self._persist()
            self._bars[key] = {
                "time": key,
                "open": float(price),
                "high": float(price),
                "low": float(price),
                "close": float(price),
                "volume": max(int(volume), 0),
            }
        else:
            bar["high"] = max(float(bar["high"]), float(price))
            bar["low"] = min(float(bar["low"]), float(price))
            bar["close"] = float(price)
            if volume > 0:
                bar["volume"] = int(bar.get("volume", 0)) + int(volume)
        self._trim_bars()

    def get_klines(self, limit: int = DEFAULT_KLINE_LIMIT) -> list[dict]:
        ordered = sorted(self._bars.values(), key=lambda b: int(b["time"]))
        if limit > 0:
            ordered = ordered[-limit:]
        return [dict(b) for b in ordered]

    def latest_bar(self) -> dict | None:
        klines = self.get_klines(limit=1)
        return klines[-1] if klines else None

    def merge_missing_bars(self, bars: list[dict]) -> int:
        """
        2026-08-25：補外部備援資料源（例如 Shioaji，見 backend/shioaji_backfill.py）
        用——群益 RequestKLineAMByDate 對「進行中交易時段」已經收盤的 K 棒不給
        資料（見檔頭常數註解），這裡讓別的資料源補這個空窗期。只寫入這裡還沒有
        資料的時間點，不覆蓋既有資料（群益回補/即時 tick 已經有的優先，備援
        資料源只補真正缺的）。整批處理完才落地存檔一次，避免補很多根時重複
        寫檔。回傳實際新增的根數。
        """
        added = 0
        for bar in bars:
            key = int(bar["time"])
            if key in self._bars:
                continue
            self._bars[key] = dict(bar)
            added += 1
        if added:
            self._trim_bars()
            self._persist()
        return added


_stores: dict[tuple[str, int], LiveKlineStore] = {}


def get_kline_store(product_code: str, interval_minutes: int = 60) -> LiveKlineStore:
    code = product_code.upper()
    key = (code, interval_minutes)
    if key not in _stores:
        _stores[key] = LiveKlineStore(code, interval_minutes=interval_minutes)
    return _stores[key]


def get_store(product_code: str) -> LiveKlineStore:
    return get_kline_store(product_code, interval_minutes=60)