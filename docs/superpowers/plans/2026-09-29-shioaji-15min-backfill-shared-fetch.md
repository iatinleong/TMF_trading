# Shioaji 15分鐘K棒回補（共用抓取）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 `backend/shioaji_backfill.py` 能同時回補 60 分鐘與 15 分鐘 K 棒，且同一個交易日的逐筆資料跨時間軸只向 Shioaji 抓一次。

**Architecture:** 把 `_expected_day_session_labels`/`_expected_night_session_labels`/`_session_query_dates`/`find_missing_bars`/`_resample_ticks_to_bars` 都加上 `interval_minutes` 參數（預設60，向後相容）。`backfill_via_shioaji()` 改成接受 `interval_minutes_list: tuple[int, ...]`，先各自算出每個時間軸的缺口，把要查詢的日期取聯集去重，對每個日期只呼叫一次 `api.ticks()`，再依「這個時間軸這天是否真的缺」個別重採樣、寫入各自的 `get_kline_store(product_code, interval_minutes=m)`，回傳 `dict[int, int]`（各時間軸各自補了幾根）。兩個既有呼叫端（`backend/api.py::repair_kline()`、`backend/live_service.py::poll_once()`）跟著更新。

**Tech Stack:** Python 3.13、pandas、pytest、既有 `backend.kline_engine.get_kline_store`（Phase 2 已泛化，不用再改）。

## Global Constraints

- 只處理 TMF（微型台指期貨）近月連續代碼，不處理大台/小台（沿用既有限制，見 spec 第四節）。
- 不改變「沒有設定 SJ_API_KEY/SJ_SEC_KEY 時退回乾等」的既有行為。
- `backfill_via_shioaji()` 的預設參數必須讓「呼叫端完全不傳 `interval_minutes_list`」時，行為（回傳型別除外）與泛化前的 60 分鐘邏輯一致——回傳型別從 `int` 改成 `dict[int, int]` 是本次唯一刻意的破壞性變更，且每個呼叫端都要跟著更新（`backend/api.py::repair_kline()` 用 `.get(60, 0)`，`backend/live_service.py::poll_once()` 改成遍歷 dict）。
- 每個新行為都要有對應的回歸測試，不能只靠既有測試「剛好還過」來當作驗證。

---

### Task 1: 泛化 `shioaji_backfill.py` 的標籤/日期/缺口偵測/重採樣函式

**Files:**
- Modify: `backend/shioaji_backfill.py:30` (import), `:88-97` (`_expected_day_session_labels`/`_expected_night_session_labels`), `:100-126` (`_session_query_dates`), `:129-153` (`find_missing_bars`), `:156-184` (`_resample_ticks_to_bars`)
- Test: `tests/test_shioaji_backfill.py`

**Interfaces:**
- Consumes: `backend.kline_engine.get_kline_store(product_code: str, interval_minutes: int = 60) -> LiveKlineStore`（已存在，不用改）、`backend.kline_engine.bar_close_time_from_ts(ts: pd.Timestamp, minutes: int = 60) -> pd.Timestamp | None`（已存在，不用改）。
- Produces（Task 2 會用到）:
  - `_expected_day_session_labels(day: pd.Timestamp, interval_minutes: int = 60) -> list[pd.Timestamp]`
  - `_expected_night_session_labels(day: pd.Timestamp, interval_minutes: int = 60) -> list[pd.Timestamp]`
  - `_session_query_dates(day: pd.Timestamp, interval_minutes: int = 60) -> list[tuple[str, pd.Timestamp]]`
  - `find_missing_bars(product_code: str, *, lookback_days: int = 2, interval_minutes: int = 60) -> list[tuple[str, pd.Timestamp]]`
  - `_resample_ticks_to_bars(df: pd.DataFrame, interval_minutes: int = 60) -> list[dict]`

- [ ] **Step 1: 寫失敗測試 — 15分鐘標籤根數與時間點**

在 `tests/test_shioaji_backfill.py` 檔案最上方 import 區塊，把：
```python
from backend.kline_engine import LiveKlineStore, get_store
```
改成：
```python
from backend.kline_engine import LiveKlineStore, get_kline_store, get_store
```

然後在 `test_expected_day_session_labels`/`test_expected_night_session_labels` 這兩個既有測試後面，新增：

```python
def test_expected_day_session_labels_15min():
    labels = _expected_day_session_labels(pd.Timestamp("2026-08-25"), interval_minutes=15)
    assert len(labels) == 20
    assert labels[0].strftime("%H:%M") == "09:00"
    assert labels[-1].strftime("%H:%M") == "13:45"


def test_expected_night_session_labels_15min():
    labels = _expected_night_session_labels(pd.Timestamp("2026-08-25"), interval_minutes=15)
    assert len(labels) == 56
    assert labels[0] == pd.Timestamp("2026-08-25 15:15:00")
    assert labels[-1] == pd.Timestamp("2026-08-26 05:00:00")
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `python -m pytest tests/test_shioaji_backfill.py -k "15min" -v`
Expected: FAIL — `_expected_day_session_labels() got an unexpected keyword argument 'interval_minutes'`（函式目前還不接受這個參數）。

- [ ] **Step 3: 泛化 `_expected_day_session_labels`/`_expected_night_session_labels`**

把 `backend/shioaji_backfill.py:88-97` 的：
```python
def _expected_day_session_labels(day: pd.Timestamp) -> list[pd.Timestamp]:
    """日盤收盤標籤 09:45..13:45（5 根），對齊 kline_engine.py 的分桶規則。"""
    base = day.normalize() + pd.Timedelta(hours=8, minutes=45)
    return [base + pd.Timedelta(minutes=60 * i) for i in range(1, 6)]


def _expected_night_session_labels(day: pd.Timestamp) -> list[pd.Timestamp]:
    """夜盤收盤標籤 16:00..次日 05:00（14 根），對齊 kline_engine.py 的分桶規則。"""
    base = day.normalize() + pd.Timedelta(hours=15)
    return [base + pd.Timedelta(minutes=60 * i) for i in range(1, 15)]
```
改成：
```python
def _expected_day_session_labels(
    day: pd.Timestamp, interval_minutes: int = 60
) -> list[pd.Timestamp]:
    """日盤收盤標籤（08:45 開盤後每 interval_minutes 分鐘一根，到 13:45 收盤），
    對齊 kline_engine.py 的分桶規則。"""
    base = day.normalize() + pd.Timedelta(hours=8, minutes=45)
    count = 300 // interval_minutes  # 日盤 08:45~13:45 共 300 分鐘
    return [base + pd.Timedelta(minutes=interval_minutes * i) for i in range(1, count + 1)]


def _expected_night_session_labels(
    day: pd.Timestamp, interval_minutes: int = 60
) -> list[pd.Timestamp]:
    """夜盤收盤標籤（15:00 開盤後每 interval_minutes 分鐘一根，到次日 05:00 收盤），
    對齊 kline_engine.py 的分桶規則。"""
    base = day.normalize() + pd.Timedelta(hours=15)
    count = 840 // interval_minutes  # 夜盤 15:00~次日05:00 共 840 分鐘
    return [base + pd.Timedelta(minutes=interval_minutes * i) for i in range(1, count + 1)]
```

- [ ] **Step 4: 執行測試確認通過**

Run: `python -m pytest tests/test_shioaji_backfill.py -k "15min or expected_day_session_labels or expected_night_session_labels" -v`
Expected: PASS（新舊測試都過，`interval_minutes=60` 預設值下行為跟改動前完全一樣：5根/14根)。

- [ ] **Step 5: 提交**

```bash
git add backend/shioaji_backfill.py tests/test_shioaji_backfill.py
git commit -m "feat(shioaji-backfill): parameterize expected session labels by interval_minutes"
```

- [ ] **Step 6: 寫失敗測試 — `_session_query_dates` 沿用 interval_minutes**

在 `tests/test_shioaji_backfill.py` 既有 `test_session_query_dates_night_session_queries_next_day` 後面新增：

```python
def test_session_query_dates_15min_produces_more_labels_same_dates():
    """15分鐘的查詢日期對照關係跟60分鐘一致（同一天日盤查當天、夜盤查隔天），
    只是每個查詢日底下的標籤數量變多（20+56 而不是 5+14）。"""
    pairs_60 = _session_query_dates(pd.Timestamp("2026-08-25"), interval_minutes=60)
    pairs_15 = _session_query_dates(pd.Timestamp("2026-08-25"), interval_minutes=15)

    query_dates_60 = {q for q, _ in pairs_60}
    query_dates_15 = {q for q, _ in pairs_15}
    assert query_dates_15 == query_dates_60  # 查詢日期集合不變，只有標籤密度變了
    assert len(pairs_15) == 76  # 20 (日盤) + 56 (夜盤)
    assert len(pairs_60) == 19  # 5 (日盤) + 14 (夜盤)
```

- [ ] **Step 7: 執行測試確認失敗**

Run: `python -m pytest tests/test_shioaji_backfill.py -k "15min_produces_more_labels" -v`
Expected: FAIL — `_session_query_dates() got an unexpected keyword argument 'interval_minutes'`。

- [ ] **Step 8: 泛化 `_session_query_dates`**

把 `backend/shioaji_backfill.py:100-126` 的：
```python
def _session_query_dates(day: pd.Timestamp) -> list[tuple[str, pd.Timestamp]]:
    """
    回傳 (Shioaji 查詢用的交易日字串, 收盤標籤) 清單，涵蓋 day 這個日曆日的
    日盤+夜盤全部理論收盤時間點。
    排除週末休市（週六與週日無當日開市的日盤與夜盤）。

    2026-08-25 實測驗證：Shioaji 的 ticks(date=X) 是「交易日」制，X 涵蓋
    「X 當天日盤 + X 前一天夜盤」；反過來說，某天日曆日的夜盤（15:00~次日
    05:00），要用「隔天」的日期去查才拿得到。日盤則直接用當天日期查即可。
    """
    result: list[tuple[str, pd.Timestamp]] = []
    weekday = day.dayofweek  # 0=Mon .. 4=Fri, 5=Sat, 6=Sun

    # 只有週一至週五 (0..4) 才有日盤 (08:45 ~ 13:45)
    if 0 <= weekday <= 4:
        day_query = day.strftime("%Y-%m-%d")
        for label in _expected_day_session_labels(day):
            result.append((day_query, label))

    # 只有週一至週五 (0..4) 才有當天開出的夜盤 (15:00 ~ 次日05:00)
    # (週五夜盤開在週五 15:00，跨到週六 05:00 結束，查詢日是週六)
    if 0 <= weekday <= 4:
        night_query = (day + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        for label in _expected_night_session_labels(day):
            result.append((night_query, label))

    return result
```
改成：
```python
def _session_query_dates(
    day: pd.Timestamp, interval_minutes: int = 60
) -> list[tuple[str, pd.Timestamp]]:
    """
    回傳 (Shioaji 查詢用的交易日字串, 收盤標籤) 清單，涵蓋 day 這個日曆日的
    日盤+夜盤全部理論收盤時間點。
    排除週末休市（週六與週日無當日開市的日盤與夜盤）。

    2026-08-25 實測驗證：Shioaji 的 ticks(date=X) 是「交易日」制，X 涵蓋
    「X 當天日盤 + X 前一天夜盤」；反過來說，某天日曆日的夜盤（15:00~次日
    05:00），要用「隔天」的日期去查才拿得到。日盤則直接用當天日期查即可。
    這個對照關係跟 interval_minutes 無關，只是標籤密度不同。
    """
    result: list[tuple[str, pd.Timestamp]] = []
    weekday = day.dayofweek  # 0=Mon .. 4=Fri, 5=Sat, 6=Sun

    # 只有週一至週五 (0..4) 才有日盤 (08:45 ~ 13:45)
    if 0 <= weekday <= 4:
        day_query = day.strftime("%Y-%m-%d")
        for label in _expected_day_session_labels(day, interval_minutes=interval_minutes):
            result.append((day_query, label))

    # 只有週一至週五 (0..4) 才有當天開出的夜盤 (15:00 ~ 次日05:00)
    # (週五夜盤開在週五 15:00，跨到週六 05:00 結束，查詢日是週六)
    if 0 <= weekday <= 4:
        night_query = (day + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        for label in _expected_night_session_labels(day, interval_minutes=interval_minutes):
            result.append((night_query, label))

    return result
```

- [ ] **Step 9: 執行測試確認通過**

Run: `python -m pytest tests/test_shioaji_backfill.py -k "session_query_dates" -v`
Expected: PASS（含既有的週末排除測試、日夜盤查詢日對照測試，全都要繼續過)。

- [ ] **Step 10: 提交**

```bash
git add backend/shioaji_backfill.py tests/test_shioaji_backfill.py
git commit -m "feat(shioaji-backfill): parameterize session query dates by interval_minutes"
```

- [ ] **Step 11: 寫失敗測試 — `find_missing_bars` 讀寫對應時間軸的 store**

在 `tests/test_shioaji_backfill.py` 既有 `test_find_missing_bars_detects_gap_in_closed_session` 後面新增：

```python
def test_find_missing_bars_checks_interval_specific_store():
    """
    interval_minutes=15 呼叫時必須讀寫 15 分鐘的 store（get_kline_store(...,
    interval_minutes=15)），不能誤讀 60 分鐘的 store——否則兩個時間軸的缺口
    偵測會互相污染（例如 15 分鐘明明缺，卻因為 60 分鐘那個時段有資料而被
    誤判成不缺)。這裡刻意讓 60 分鐘的 store 保持全空，驗證它完全沒被動到。
    """
    product = "TESTINTERVALGAP"
    store_15 = get_kline_store(product, interval_minutes=15)
    store_15._bars.clear()
    store_60 = get_kline_store(product, interval_minutes=60)
    store_60._bars.clear()

    base_day = pd.Timestamp.now().normalize() - pd.Timedelta(days=3)
    while base_day.dayofweek >= 5:
        base_day -= pd.Timedelta(days=1)
    all_labels = _expected_day_session_labels(base_day, interval_minutes=15)
    present_labels = [label for label in all_labels if label.strftime("%H:%M") != "10:00"]
    for label in present_labels:
        key = to_unix_seconds(label)
        store_15._bars[key] = {
            "time": key, "open": 100.0, "high": 100.0, "low": 100.0,
            "close": 100.0, "volume": 1,
        }

    missing = find_missing_bars(product, lookback_days=6, interval_minutes=15)
    missing_on_base_day = [
        label for _, label in missing
        if label.normalize() == base_day and label.strftime("%H:%M") in
        [t.strftime("%H:%M") for t in all_labels]
    ]
    assert len(missing_on_base_day) == 1
    assert missing_on_base_day[0].strftime("%H:%M") == "10:00"
    assert store_60._bars == {}  # 60分鐘的 store 完全沒被動到
```

- [ ] **Step 12: 執行測試確認失敗**

Run: `python -m pytest tests/test_shioaji_backfill.py -k "interval_specific_store" -v`
Expected: FAIL — `find_missing_bars() got an unexpected keyword argument 'interval_minutes'`。

- [ ] **Step 13: 泛化 `find_missing_bars`**

把 `backend/shioaji_backfill.py:30` 的 import 行：
```python
from .kline_engine import bar_close_time_from_ts, get_store
```
改成：
```python
from .kline_engine import bar_close_time_from_ts, get_kline_store
```

把 `backend/shioaji_backfill.py:129-153` 的：
```python
def find_missing_bars(
    product_code: str, *, lookback_days: int = 2
) -> list[tuple[str, pd.Timestamp]]:
    """
    列出最近 lookback_days 天內，已經收盤、但目前 store 裡缺的 K 棒。
    只檢查「已經過去」的收盤時間點（還沒開始/正在成型中的那一根不算缺）。
    回傳 (Shioaji 查詢用日期字串, 收盤標籤) 清單。
    """
    store = get_store(product_code)
    existing = {int(b["time"]) for b in store.get_klines(limit=0)}

    # 2026-08-30 修正：使用台北時間（Naive Asia/Taipei），不能用裸 pd.Timestamp.now()
    # 否則在 UTC 系統時區的 VM 上，now 會慢 8 小時，導致當天白天的 K 棒全被
    # 誤判成「未來的時間」而漏補。
    now = pd.Timestamp.now(TAIPEI_TZ).tz_localize(None)
    missing: list[tuple[str, pd.Timestamp]] = []
    for delta in range(lookback_days, -1, -1):
        day = now.normalize() - pd.Timedelta(days=delta)
        for query_date, label in _session_query_dates(day):
            if label >= now:
                continue
            key = to_unix_seconds(label)
            if key not in existing:
                missing.append((query_date, label))
    return missing
```
改成：
```python
def find_missing_bars(
    product_code: str, *, lookback_days: int = 2, interval_minutes: int = 60
) -> list[tuple[str, pd.Timestamp]]:
    """
    列出最近 lookback_days 天內，已經收盤、但目前 store 裡缺的 K 棒。
    只檢查「已經過去」的收盤時間點（還沒開始/正在成型中的那一根不算缺）。
    回傳 (Shioaji 查詢用日期字串, 收盤標籤) 清單。讀的是 interval_minutes
    對應的那個 store（get_kline_store 內部依 (code, interval_minutes) 分開
    快取，不會跟其他時間軸的資料混在一起）。
    """
    store = get_kline_store(product_code, interval_minutes=interval_minutes)
    existing = {int(b["time"]) for b in store.get_klines(limit=0)}

    # 2026-08-30 修正：使用台北時間（Naive Asia/Taipei），不能用裸 pd.Timestamp.now()
    # 否則在 UTC 系統時區的 VM 上，now 會慢 8 小時，導致當天白天的 K 棒全被
    # 誤判成「未來的時間」而漏補。
    now = pd.Timestamp.now(TAIPEI_TZ).tz_localize(None)
    missing: list[tuple[str, pd.Timestamp]] = []
    for delta in range(lookback_days, -1, -1):
        day = now.normalize() - pd.Timedelta(days=delta)
        for query_date, label in _session_query_dates(day, interval_minutes=interval_minutes):
            if label >= now:
                continue
            key = to_unix_seconds(label)
            if key not in existing:
                missing.append((query_date, label))
    return missing
```

**注意（同一步驟內一併處理，避免中間留下壞掉的提交)**：Step 13 把這個檔案的 import 從 `get_store` 換成 `get_kline_store` 後，`backend/shioaji_backfill.py:262`（`backfill_via_shioaji` 內部)的 `store = get_store(product_code)` 會找不到 `get_store` 而炸掉 `NameError`。Task 2 會整個重寫 `backfill_via_shioaji`，但在那之前，這裡先把這一行也改成等價的：
```python
            store = get_kline_store(product_code, interval_minutes=60)
```
（單純把舊呼叫換成新函式、固定傳 60，行為完全不變——這只是讓這個檔案在 Task 2 動手之前，每個提交點都是可以直接跑測試通過的完整狀態，不是搶先實作 Task 2 的邏輯。)

- [ ] **Step 14: 執行測試確認通過**

Run: `python -m pytest tests/test_shioaji_backfill.py -v`
Expected: 全部 PASS——包含 `find_missing_bars` 本身的泛化，以及 `backfill_via_shioaji` 因為上面那個相容性替換而繼續正常運作（回傳值目前還是舊的 `int`，Task 2 才會改成 `dict`)。

- [ ] **Step 15: 提交**

```bash
git add backend/shioaji_backfill.py tests/test_shioaji_backfill.py
git commit -m "feat(shioaji-backfill): parameterize find_missing_bars by interval_minutes"
```

- [ ] **Step 16: 寫失敗測試 — `_resample_ticks_to_bars` 依 interval_minutes 分桶**

在 `tests/test_shioaji_backfill.py` 既有 `test_resample_ticks_to_bars_sorts_unsorted_ticks` 後面新增：

```python
def test_resample_ticks_to_bars_15min_buckets_are_narrower():
    """15分鐘分桶下，跨越 09:46~10:00 跟 10:01~10:15 的 tick 應該落在不同兩根，
    不像 60 分鐘那樣全部併成一根——驗證 interval_minutes 真的有傳到分桶邏輯。"""
    from backend.shioaji_backfill import _resample_ticks_to_bars

    df = pd.DataFrame(
        [
            {"ts": "2026-08-25 09:50:00", "close": 21500.0, "volume": 5},
            {"ts": "2026-08-25 10:10:00", "close": 21600.0, "volume": 10},
        ]
    )
    bars_60 = _resample_ticks_to_bars(df, interval_minutes=60)
    bars_15 = _resample_ticks_to_bars(df, interval_minutes=15)

    assert len(bars_60) == 1  # 60分鐘下兩筆 tick 同屬 10:45 那根
    assert len(bars_15) == 2  # 15分鐘下分屬 10:00 跟 10:15 兩根
```

- [ ] **Step 17: 執行測試確認失敗**

Run: `python -m pytest tests/test_shioaji_backfill.py -k "15min_buckets_are_narrower" -v`
Expected: FAIL — `_resample_ticks_to_bars() got an unexpected keyword argument 'interval_minutes'`。

- [ ] **Step 18: 泛化 `_resample_ticks_to_bars`**

把 `backend/shioaji_backfill.py:156-184` 的：
```python
def _resample_ticks_to_bars(df: pd.DataFrame) -> list[dict]:
    df = df.copy()
    df["ts"] = pd.to_datetime(df["ts"])
    # 2026-08-30 修正：按時間排序，避免 API 回傳亂序時 open/close 抓錯第一筆/最後一筆
    df = df.sort_values("ts")
    df["bar_close"] = df["ts"].apply(bar_close_time_from_ts)
    df = df.dropna(subset=["bar_close"])
```
改成：
```python
def _resample_ticks_to_bars(df: pd.DataFrame, interval_minutes: int = 60) -> list[dict]:
    df = df.copy()
    df["ts"] = pd.to_datetime(df["ts"])
    # 2026-08-30 修正：按時間排序，避免 API 回傳亂序時 open/close 抓錯第一筆/最後一筆
    df = df.sort_values("ts")
    df["bar_close"] = df["ts"].apply(
        lambda ts: bar_close_time_from_ts(ts, minutes=interval_minutes)
    )
    df = df.dropna(subset=["bar_close"])
```
（函式其餘部分——`groupby`/回傳格式——不變。)

- [ ] **Step 19: 執行測試確認通過**

Run: `python -m pytest tests/test_shioaji_backfill.py -v`
Expected: 跟 Step 14 一樣的中間狀態——`_resample_ticks_to_bars` 相關測試全過，`backfill_via_shioaji` 相關測試仍因為 `get_store` 未定義而失敗（留給 Task 2 修)。

- [ ] **Step 20: 提交**

```bash
git add backend/shioaji_backfill.py tests/test_shioaji_backfill.py
git commit -m "feat(shioaji-backfill): parameterize tick resampling by interval_minutes"
```

---

### Task 2: 重寫 `backfill_via_shioaji()` 為共用抓取、多時間軸版本

**Files:**
- Modify: `backend/shioaji_backfill.py:187-277`（整個 `backfill_via_shioaji` 函式)
- Test: `tests/test_shioaji_backfill.py`

**Interfaces:**
- Consumes（Task 1 已產出): `find_missing_bars(product_code, *, lookback_days=2, interval_minutes=60)`、`_resample_ticks_to_bars(df, interval_minutes=60)`、`get_kline_store(product_code, interval_minutes=60)`（來自 `backend.kline_engine`，Task 1 已把 import 換成這個)。
- Produces（Task 3 會用到): `backfill_via_shioaji(product_code: str, *, lookback_days: int = 2, interval_minutes_list: tuple[int, ...] = (60,)) -> dict[int, int]` — key 是 `interval_minutes_list` 裡的每個值，value 是該時間軸實際補進去的根數。

- [ ] **Step 1: 更新既有測試對回傳型別的假設（int → dict）**

這兩個既有測試目前假設 `backfill_via_shioaji(...)` 回傳單一 `int`。把 `tests/test_shioaji_backfill.py` 裡：
```python
def test_backfill_via_shioaji_logs_why_it_skipped_when_no_keys(tmp_path, monkeypatch):
    ...
    added = backfill_via_shioaji("TESTAUDITSKIP")

    assert added == 0
```
改成：
```python
def test_backfill_via_shioaji_logs_why_it_skipped_when_no_keys(tmp_path, monkeypatch):
    ...
    added = backfill_via_shioaji("TESTAUDITSKIP")

    assert added == {60: 0}
```

以及：
```python
def test_backfill_via_shioaji_logs_login_failure(tmp_path, monkeypatch):
    ...
    with patch.dict("sys.modules", {"shioaji": fake_module}):
        added = backfill_via_shioaji("TESTAUDITLOGIN")

    assert added == 0
```
改成：
```python
def test_backfill_via_shioaji_logs_login_failure(tmp_path, monkeypatch):
    ...
    with patch.dict("sys.modules", {"shioaji": fake_module}):
        added = backfill_via_shioaji("TESTAUDITLOGIN")

    assert added == {60: 0}
```
（`...` 代表這兩個測試函式中間不變的部分，只改最後的 assert。)

- [ ] **Step 2: 寫失敗測試 — 兩個時間軸都缺同一天，只查詢一次 Shioaji**

在 `tests/test_shioaji_backfill.py` 檔案最後新增：

```python
def test_backfill_via_shioaji_shares_one_fetch_across_intervals(tmp_path, monkeypatch):
    """
    核心優化驗證：60分鐘跟15分鐘都缺同一個交易日時，api.ticks() 只能被呼叫
    一次（不是各自呼叫各打一次)，但兩個時間軸各自的 store 都要拿到補進去
    的資料。這是這次重構唯一真正的行為變更，必須有回歸測試釘住,不能只靠
    看 code review。
    """
    audit_path = tmp_path / "shioaji_backfill_audit.log"
    monkeypatch.setattr(shioaji_backfill, "_audit_log_path", lambda: audit_path)
    monkeypatch.setattr(shioaji_backfill, "shioaji_available", lambda: True)
    monkeypatch.setattr(shioaji_backfill, "_is_production", lambda: True)

    product = "TESTSHAREDFETCH"
    store_60 = get_kline_store(product, interval_minutes=60)
    store_60._bars.clear()
    store_15 = get_kline_store(product, interval_minutes=15)
    store_15._bars.clear()

    # 兩個時間軸都在同一天（2026-08-26）缺一根。
    monkeypatch.setattr(
        shioaji_backfill,
        "find_missing_bars",
        lambda product_code, *, lookback_days, interval_minutes: (
            [("2026-08-26", pd.Timestamp("2026-08-26 09:45"))]
            if interval_minutes == 60
            else [("2026-08-26", pd.Timestamp("2026-08-26 09:00"))]
        ),
    )

    ticks_call_count = {"n": 0}

    class _FakeTicks:
        def dict(self):
            return [{"ts": "2026-08-26 09:30:00", "close": 21000.0, "volume": 1}]

    class _FakeApi:
        Contracts = type(
            "C", (), {"Futures": type("F", (), {"TMF": type("T", (), {"TMFR1": object()})()})()}
        )()

        def login(self, **kwargs):
            pass

        def ticks(self, contract, date):
            ticks_call_count["n"] += 1
            return _FakeTicks()

        def logout(self):
            pass

    fake_module = type("FakeShioaji", (), {"Shioaji": lambda self, simulation: _FakeApi()})()

    with patch.dict("sys.modules", {"shioaji": fake_module}):
        added = backfill_via_shioaji(product, interval_minutes_list=(60, 15))

    assert ticks_call_count["n"] == 1  # 同一天只查一次，不是兩次
    assert added[60] == 1
    assert added[15] == 1
```

- [ ] **Step 3: 執行測試確認失敗**

Run: `python -m pytest tests/test_shioaji_backfill.py -k "shares_one_fetch" -v`
Expected: FAIL — 目前 `backfill_via_shioaji` 不接受 `interval_minutes_list` 參數（`TypeError: backfill_via_shioaji() got an unexpected keyword argument 'interval_minutes_list'`）。

- [ ] **Step 4: 寫失敗測試 — 只有一個時間軸缺某天時，另一個時間軸不會被誤補**

緊接著上面那個測試，新增：

```python
def test_backfill_via_shioaji_only_updates_intervals_that_were_actually_missing(tmp_path, monkeypatch):
    """
    某一天如果只有 15 分鐘缺、60 分鐘不缺，抓回來的那份逐筆資料不該被拿去
    補 60 分鐘的 store——不然等於無條件覆蓋式回補，跟 merge_missing_bars
    『只補真正缺的』這個既有語意（見 kline_engine.py 檔頭註解)矛盾。
    """
    audit_path = tmp_path / "shioaji_backfill_audit.log"
    monkeypatch.setattr(shioaji_backfill, "_audit_log_path", lambda: audit_path)
    monkeypatch.setattr(shioaji_backfill, "shioaji_available", lambda: True)
    monkeypatch.setattr(shioaji_backfill, "_is_production", lambda: True)

    product = "TESTPARTIALFETCH"
    store_60 = get_kline_store(product, interval_minutes=60)
    store_60._bars.clear()
    store_15 = get_kline_store(product, interval_minutes=15)
    store_15._bars.clear()

    # 只有 15 分鐘缺 2026-08-26；60 分鐘完全沒有缺口（回傳空清單）。
    monkeypatch.setattr(
        shioaji_backfill,
        "find_missing_bars",
        lambda product_code, *, lookback_days, interval_minutes: (
            [] if interval_minutes == 60 else [("2026-08-26", pd.Timestamp("2026-08-26 09:00"))]
        ),
    )

    class _FakeTicks:
        def dict(self):
            return [{"ts": "2026-08-26 09:30:00", "close": 21000.0, "volume": 1}]

    class _FakeApi:
        Contracts = type(
            "C", (), {"Futures": type("F", (), {"TMF": type("T", (), {"TMFR1": object()})()})()}
        )()

        def login(self, **kwargs):
            pass

        def ticks(self, contract, date):
            return _FakeTicks()

        def logout(self):
            pass

    fake_module = type("FakeShioaji", (), {"Shioaji": lambda self, simulation: _FakeApi()})()

    with patch.dict("sys.modules", {"shioaji": fake_module}):
        added = backfill_via_shioaji(product, interval_minutes_list=(60, 15))

    assert added[60] == 0  # 60分鐘本來就不缺，不該被動到
    assert added[15] == 1
    assert store_60._bars == {}  # 60分鐘的 store 完全沒被寫入任何資料
```

- [ ] **Step 5: 執行測試確認失敗**

Run: `python -m pytest tests/test_shioaji_backfill.py -k "only_updates_intervals" -v`
Expected: FAIL — 同上，`interval_minutes_list` 參數不存在。

- [ ] **Step 6: 重寫 `backfill_via_shioaji`**

把 `backend/shioaji_backfill.py` 裡的整個 `backfill_via_shioaji` 函式（Task 1 Step 13 已經把內部的 `get_store(product_code)` 換成 `get_kline_store(product_code, interval_minutes=60)`，下面這段「舊」程式碼是那次小修改後的實際狀態，不是最初讀到的版本）：
```python
def backfill_via_shioaji(product_code: str, *, lookback_days: int = 2) -> int:
    """
    檢查缺口，有缺才連線 Shioaji 補；沒有缺口/沒設定金鑰/連線失敗都安靜跳過，
    回傳實際補進去的根數（0 不代表失敗，可能就是沒有缺口)。每一步都落地
    記一筆稽核 log（見 _append_audit 說明），失敗時才查得出卡在哪一步。
    """
    available = shioaji_available()
    if not available:
        _append_audit(f"product={product_code}\tskip=沒有可用的 SJ_API_KEY/SJ_SEC_KEY")
        return 0

    missing = find_missing_bars(product_code, lookback_days=lookback_days)
    if not missing:
        _append_audit(f"product={product_code}\tskip=沒有偵測到缺口")
        return 0

    _append_audit(f"product={product_code}\tmissing={len(missing)}\t開始連線查詢")
    logger.info("Shioaji 補缺：偵測到 %d 根缺口，準備連線查詢 %s", len(missing), product_code)

    try:
        import shioaji as sj
    except ImportError as exc:
        _append_audit(f"product={product_code}\tfail=shioaji 套件未安裝: {exc}")
        logger.warning("shioaji 套件未安裝，無法補缺口")
        return 0

    api = sj.Shioaji(simulation=not _is_production())
    added_total = 0
    try:
        try:
            api.login(
                api_key=os.getenv("SJ_API_KEY", "").strip(),
                secret_key=os.getenv("SJ_SEC_KEY", "").strip(),
            )
        except Exception as exc:  # noqa: BLE001
            _append_audit(f"product={product_code}\tfail=login 失敗: {exc}")
            return 0

        try:
            contract = api.Contracts.Futures.TMF.TMFR1
        except Exception as exc:  # noqa: BLE001
            _append_audit(f"product={product_code}\tfail=取得合約失敗: {exc}")
            return 0

        query_dates = sorted({query_date for query_date, _ in missing})
        for query_date in query_dates:
            try:
                ticks = api.ticks(contract=contract, date=query_date)
            except Exception as exc:  # noqa: BLE001
                _append_audit(f"product={product_code}\tquery={query_date}\tfail=查詢失敗: {exc}")
                logger.warning("Shioaji 查詢 %s 失敗: %s", query_date, exc)
                continue
            df = pd.DataFrame(ticks.dict())
            if df.empty:
                _append_audit(f"product={product_code}\tquery={query_date}\tresult=空資料")
                continue
            bars = _resample_ticks_to_bars(df)
            if not bars:
                _append_audit(
                    f"product={product_code}\tquery={query_date}\t"
                    f"result=有 {len(df)} 筆 tick 但 resample 不出任何 K 棒（可能都不在交易時段內）"
                )
                continue
            store = get_kline_store(product_code, interval_minutes=60)
            added = store.merge_missing_bars(bars)
            added_total += added
            _append_audit(
                f"product={product_code}\tquery={query_date}\tticks={len(df)}\t"
                f"resampled={len(bars)}\tadded={added}"
            )
            logger.info("Shioaji 補缺：%s 查詢新增 %d 根", query_date, added)
    finally:
        try:
            api.logout()
        except Exception:  # noqa: BLE001
            pass

    _append_audit(f"product={product_code}\t完成\tadded_total={added_total}")
    return added_total
```
整個換成：
```python
def backfill_via_shioaji(
    product_code: str,
    *,
    lookback_days: int = 2,
    interval_minutes_list: tuple[int, ...] = (60,),
) -> dict[int, int]:
    """
    檢查每個時間軸各自的缺口，有缺才連線 Shioaji 補；沒有缺口/沒設定金鑰/
    連線失敗都安靜跳過。回傳 {interval_minutes: 實際補進去的根數}（每個
    interval_minutes_list 裡的值都保證有 key，0 不代表失敗，可能就是沒有
    缺口)。

    2026-09-30：改成一次處理多個時間軸、共用同一次抓回來的逐筆資料——
    Shioaji ticks() 本來就是抓「整個交易日」的逐筆資料，重採樣成幾分鐘
    一根純粹是本機運算，不影響抓幾次。這裡把每個時間軸各自的缺口日期
    取聯集去重，同一天只查一次，再依「這個時間軸這天是否真的缺」個別
    重採樣、寫入各自的 store，避免同一天被不同時間軸各自呼叫各打一次
    api.ticks()。

    每一步都落地記一筆稽核 log（見 _append_audit 說明），失敗時才查得出
    卡在哪一步。
    """
    added_by_interval: dict[int, int] = {m: 0 for m in interval_minutes_list}

    available = shioaji_available()
    if not available:
        _append_audit(f"product={product_code}\tskip=沒有可用的 SJ_API_KEY/SJ_SEC_KEY")
        return added_by_interval

    missing_by_interval = {
        m: find_missing_bars(product_code, lookback_days=lookback_days, interval_minutes=m)
        for m in interval_minutes_list
    }
    query_dates_by_interval = {
        m: {query_date for query_date, _ in missing} for m, missing in missing_by_interval.items()
    }
    all_query_dates = sorted({qd for dates in query_dates_by_interval.values() for qd in dates})
    if not all_query_dates:
        _append_audit(f"product={product_code}\tskip=沒有偵測到缺口")
        return added_by_interval

    total_missing = sum(len(m) for m in missing_by_interval.values())
    _append_audit(f"product={product_code}\tmissing={total_missing}\t開始連線查詢")
    logger.info("Shioaji 補缺：偵測到 %d 根缺口，準備連線查詢 %s", total_missing, product_code)

    try:
        import shioaji as sj
    except ImportError as exc:
        _append_audit(f"product={product_code}\tfail=shioaji 套件未安裝: {exc}")
        logger.warning("shioaji 套件未安裝，無法補缺口")
        return added_by_interval

    api = sj.Shioaji(simulation=not _is_production())
    try:
        try:
            api.login(
                api_key=os.getenv("SJ_API_KEY", "").strip(),
                secret_key=os.getenv("SJ_SEC_KEY", "").strip(),
            )
        except Exception as exc:  # noqa: BLE001
            _append_audit(f"product={product_code}\tfail=login 失敗: {exc}")
            return added_by_interval

        try:
            contract = api.Contracts.Futures.TMF.TMFR1
        except Exception as exc:  # noqa: BLE001
            _append_audit(f"product={product_code}\tfail=取得合約失敗: {exc}")
            return added_by_interval

        for query_date in all_query_dates:
            try:
                ticks = api.ticks(contract=contract, date=query_date)
            except Exception as exc:  # noqa: BLE001
                _append_audit(f"product={product_code}\tquery={query_date}\tfail=查詢失敗: {exc}")
                logger.warning("Shioaji 查詢 %s 失敗: %s", query_date, exc)
                continue
            df = pd.DataFrame(ticks.dict())
            if df.empty:
                _append_audit(f"product={product_code}\tquery={query_date}\tresult=空資料")
                continue

            for m in interval_minutes_list:
                if query_date not in query_dates_by_interval[m]:
                    continue  # 這個時間軸這天沒缺，不用重複補
                bars = _resample_ticks_to_bars(df, interval_minutes=m)
                if not bars:
                    _append_audit(
                        f"product={product_code}\tquery={query_date}\tinterval={m}\t"
                        f"result=有 {len(df)} 筆 tick 但 resample 不出任何 K 棒（可能都不在交易時段內）"
                    )
                    continue
                store = get_kline_store(product_code, interval_minutes=m)
                added = store.merge_missing_bars(bars)
                added_by_interval[m] += added
                _append_audit(
                    f"product={product_code}\tquery={query_date}\tinterval={m}\tticks={len(df)}\t"
                    f"resampled={len(bars)}\tadded={added}"
                )
                logger.info("Shioaji 補缺(%d分鐘)：%s 查詢新增 %d 根", m, query_date, added)
    finally:
        try:
            api.logout()
        except Exception:  # noqa: BLE001
            pass

    _append_audit(f"product={product_code}\t完成\tadded_by_interval={added_by_interval}")
    return added_by_interval
```

- [ ] **Step 7: 執行測試確認通過**

Run: `python -m pytest tests/test_shioaji_backfill.py -v`
Expected: 全部 PASS，包含 Task 1 留下的、原本因為 `get_store` 未定義而失敗的測試（現在都改用 `get_kline_store` 了)。

- [ ] **Step 8: 提交**

```bash
git add backend/shioaji_backfill.py tests/test_shioaji_backfill.py
git commit -m "feat(shioaji-backfill): share one Shioaji fetch across multiple backfill intervals"
```

---

### Task 3: 更新兩個呼叫端（`repair_kline`、`poll_once`）

**Files:**
- Modify: `backend/api.py:597-619`（`repair_kline` 函式)
- Modify: `backend/live_service.py:370-377`（`poll_once` 裡的 Shioaji 定期補缺區塊)
- Test: 沿用既有 `tests/test_live_service_shioaji_trigger.py`（不改，這個檔案只測節流判斷函式，跟本次呼叫端變更無關，見下方 Step 5 的驗證)

**Interfaces:**
- Consumes（Task 2 已產出): `backfill_via_shioaji(product_code: str, *, lookback_days: int = 2, interval_minutes_list: tuple[int, ...] = (60,)) -> dict[int, int]`

- [ ] **Step 1: 更新 `repair_kline()` 的呼叫方式**

把 `backend/api.py:597-620` 的：
```python
    from .kline_engine import get_store
    from .shioaji_backfill import backfill_via_shioaji
    from .strategy_service import resolve_strategy_product_code

    prod = resolve_strategy_product_code(product)
    store = get_store(prod)
    removed_key = None
    if bar_time:
        if bar_time in store._bars:
            store._bars.pop(bar_time, None)
            removed_key = bar_time
    else:
        if store._bars:
            removed_key = max(store._bars.keys())
            store._bars.pop(removed_key, None)
    if removed_key is not None:
        store._persist()
    added = backfill_via_shioaji(prod, lookback_days=1)
    return {
        "product": prod,
        "removed_bar_time": removed_key,
        "backfilled_bars": added,
        "status": "SUCCESS",
    }
```
改成：
```python
    from .kline_engine import get_store
    from .shioaji_backfill import backfill_via_shioaji
    from .strategy_service import resolve_strategy_product_code

    prod = resolve_strategy_product_code(product)
    store = get_store(prod)
    removed_key = None
    if bar_time:
        if bar_time in store._bars:
            store._bars.pop(bar_time, None)
            removed_key = bar_time
    else:
        if store._bars:
            removed_key = max(store._bars.keys())
            store._bars.pop(removed_key, None)
    if removed_key is not None:
        store._persist()
    # 這個端點是手動熱修復「60分鐘」K棒用的（見 get_store 固定回傳 60分鐘的
    # store)，backfill_via_shioaji 泛化成多時間軸、回傳 dict 之後，這裡只取
    # 60分鐘那個 key，行為跟泛化前完全一樣，不因此開始處理15分鐘。
    added = backfill_via_shioaji(prod, lookback_days=1).get(60, 0)
    return {
        "product": prod,
        "removed_bar_time": removed_key,
        "backfilled_bars": added,
        "status": "SUCCESS",
    }
```

- [ ] **Step 2: 更新 `poll_once()` 裡的 Shioaji 定期補缺呼叫**

把 `backend/live_service.py:367-377` 的：
```python
    now = time.time()
    if not _is_worker_mode() and _should_attempt_periodic_shioaji_backfill(now, _last_shioaji_backfill_attempt):
        _last_shioaji_backfill_attempt = now
        try:
            from .shioaji_backfill import backfill_via_shioaji

            added = await asyncio.to_thread(backfill_via_shioaji, quote_product)
            if added:
                logger.info("Shioaji 定期補缺：%s 新增 %d 根", quote_product, added)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Shioaji 定期補缺失敗（不影響主流程）: %s", exc)
```
改成：
```python
    now = time.time()
    if not _is_worker_mode() and _should_attempt_periodic_shioaji_backfill(now, _last_shioaji_backfill_attempt):
        _last_shioaji_backfill_attempt = now
        try:
            from .shioaji_backfill import backfill_via_shioaji

            # 2026-09-30：60分鐘/15分鐘共用同一個節流節奏——backfill_via_shioaji
            # 內部同一天的缺口跨時間軸只會真的查詢 Shioaji 一次，不需要各自
            # 獨立的節流計時器。
            added_by_interval = await asyncio.to_thread(
                backfill_via_shioaji, quote_product, interval_minutes_list=(60, 15)
            )
            for interval_minutes, added in added_by_interval.items():
                if added:
                    logger.info(
                        "Shioaji 定期補缺(%d分鐘)：%s 新增 %d 根",
                        interval_minutes, quote_product, added,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Shioaji 定期補缺失敗（不影響主流程）: %s", exc)
```

- [ ] **Step 3: 確認 `repair_kline` 的呼叫端行為不變 — 手動驗證既有測試涵蓋範圍**

檢查是否已有針對 `/api/klines/repair` 端點的測試：

Run: `grep -rn "klines/repair\|repair_kline" tests/`

若沒有任何測試涵蓋這個端點（這是既有狀況，不是這次改動造成的），不需要為了這次的小改動臨時新增端點測試——`.get(60, 0)` 是純粹的型別配合，行為不變，已經由 Task 2 的 `test_backfill_via_shioaji_only_updates_intervals_that_were_actually_missing`（驗證 `added[60]` 語意正確)間接覆蓋。若指令找到既有測試，直接執行確認仍然通過：

Run: `python -m pytest <該測試檔案路徑> -v`
Expected: PASS。

- [ ] **Step 4: 執行完整測試套件**

Run: `python -m pytest tests/ -v`
Expected: 全部 PASS，特別留意：
- `tests/test_shioaji_backfill.py`（Task 1+2 修改的核心)
- `tests/test_live_service_shioaji_trigger.py`（驗證 `_should_attempt_periodic_shioaji_backfill` 本身沒被動到，這個節流判斷函式在這次改動裡完全沒變)
- `tests/test_api.py` 或其他涵蓋 `backend/api.py` 的測試檔（若存在，確認 `repair_kline` 相關測試沒有因為呼叫簽名改變而破版)

- [ ] **Step 5: 提交**

```bash
git add backend/api.py backend/live_service.py
git commit -m "feat(shioaji-backfill): wire repair_kline and poll_once to the multi-interval backfill signature"
```

- [ ] **Step 6: 更新 spec 文件狀態（若有「實作狀態」欄位的慣例，否則跳過）**

檢查 `docs/superpowers/specs/2026-09-29-death-cross-shioaji-15min-backfill-design.md` 開頭是否有這個專案慣用的「狀態: 待實作/已完成」標記慣例（可用 `grep -n "狀態\|Status" docs/superpowers/specs/*.md` 查其他 spec 檔案是否有這個模式)。若有，比照更新；若沒有這個慣例，跳過這一步，不要無中生有加一個新的文件慣例。

---

## 完成後驗證（不是新任務，是整個 plan 跑完後的最終確認）

- [ ] 執行 `python -m pytest tests/ -v`，確認全部通過。
- [ ] 執行 `grep -n "get_store(product_code)" backend/shioaji_backfill.py`，確認 `backfill_via_shioaji` 內部已經沒有殘留舊的 60分鐘寫死呼叫（應該只剩 `get_kline_store` 呼叫)。
- [ ] 確認 `backend/api.py` 的 `repair_kline()` 跟 `backend/live_service.py` 的 `poll_once()` 都改用新簽名，且沒有其他檔案還在呼叫舊的 `backfill_via_shioaji(product_code, lookback_days=N)`（不帶 `interval_minutes_list`、指望拿到 `int`)這種舊用法：`grep -rn "backfill_via_shioaji(" backend/`。
