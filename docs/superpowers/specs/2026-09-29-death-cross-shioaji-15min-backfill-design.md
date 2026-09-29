# 死叉做空策略：Shioaji 15分鐘K棒回補 設計文件

## 背景與目標

Phase 2（`docs/superpowers/specs/2026-09-29-death-cross-live-trading-phase2-design.md`）把死叉做空策略接上即時交易後，發現一個真實的營運代價：15 分鐘即時K棒沒有官方回補管道（群益不提供任意週期回補),只能靠即時 tick 現場累積。這代表**每次遇到全新合約代碼**（第一次上線 + 之後每個月結算換約),都要等 22 根K棒（約5.5小時)才會開始出訊號——而且不是一次性的，是每月都會重來一次（本機快取是用商品代碼命名，換約後代碼變了，等於全新、空的快取)。

這份文件的目標：用專案裡本來就有的永豐 Shioaji 逐筆資料回補機制（`backend/shioaji_backfill.py`，目前只支援60分鐘),把它泛化成也能回補15分鐘K棒，換約當下直接把缺口補齊，不用再乾等。

**這是 Phase 2 的獨立後續工作，依賴 Phase 2 Task 1 已經完成的 `get_kline_store`/`bar_close_time_from_ts` 泛化**，執行順序上排在 Phase 2 四個任務之後。

## 現有架構（已核對）

`backend/shioaji_backfill.py` 現有 4 個函式，全部把 60 分鐘桶寫死在裡面：
- `_expected_day_session_labels(day)`/`_expected_night_session_labels(day)`：日盤 300 分鐘產生 5 個 60 分鐘標籤（`range(1,6)`)、夜盤 840 分鐘產生 14 個標籤（`range(1,15)`)。
- `_session_query_dates(day)`：組合日盤+夜盤要查詢 Shioaji 的（日期, 標籤)清單。
- `find_missing_bars(product_code, *, lookback_days=2)`：用 `get_store(product_code)`（寫死60分鐘)讀現況、比對缺口。
- `_resample_ticks_to_bars(df)`：用 `bar_close_time_from_ts(ts)`（預設60分鐘)把 Shioaji 逐筆資料分桶。
- `backfill_via_shioaji(product_code, *, lookback_days=2)`：串起以上邏輯,查詢 Shioaji ticks、寫回 `get_store(product_code).merge_missing_bars(bars)`。

觸發端 `backend/live_service.py::poll_once()`：獨立於群益報價就緒與否，每 300 秒（`SHIOAJI_BACKFILL_INTERVAL_SECONDS`)無條件檢查一次是否有缺口，用 `_last_shioaji_backfill_attempt` 這個模組層級變數節流。

## 一、`shioaji_backfill.py` 泛化

沿用這個 session 已經用過兩次的「抽出參數、預設值保持原行為」模式：

```python
def _expected_day_session_labels(day: pd.Timestamp, interval_minutes: int = 60) -> list[pd.Timestamp]:
    base = day.normalize() + pd.Timedelta(hours=8, minutes=45)
    count = 300 // interval_minutes  # 日盤 08:45~13:45 共 300 分鐘
    return [base + pd.Timedelta(minutes=interval_minutes * i) for i in range(1, count + 1)]


def _expected_night_session_labels(day: pd.Timestamp, interval_minutes: int = 60) -> list[pd.Timestamp]:
    base = day.normalize() + pd.Timedelta(hours=15)
    count = 840 // interval_minutes  # 夜盤 15:00~次日05:00 共 840 分鐘
    return [base + pd.Timedelta(minutes=interval_minutes * i) for i in range(1, count + 1)]
```
`interval_minutes=60` 時：`300//60=5`、`840//60=14`，跟現有行為完全一致。`interval_minutes=15` 時：`300//15=20`、`840//15=56`，跟 Phase 1/2 已經驗證過的根數一致。

`_session_query_dates(day, interval_minutes=60)`、`find_missing_bars(product_code, *, lookback_days=2, interval_minutes=60)`、`_resample_ticks_to_bars(df, interval_minutes=60)` 都往下多加同一個參數並繼續傳遞，跟 Phase 2 一樣的模式，這幾個不變。`find_missing_bars` 內部原本呼叫 `get_store(product_code)` 的地方，改成 `get_kline_store(product_code, interval_minutes=interval_minutes)`（`get_store` 這個名字在這支檔案裡不再需要，import 只留 `get_kline_store`)。

**`backfill_via_shioaji` 改成一次處理多個時間軸、共用同一次抓回來的逐筆資料**（設計討論時確認的優化：60分鐘/15分鐘各自獨立呼叫的話，同一天的缺口可能被各自呼叫各打一次 `ticks()`，雖然遠低於官方流量/次數限制不會真的出問題，但沒必要浪費)。查證過官方文件（https://sinotrade.github.io/zh/tutor/limit/）：`ticks()` 是**整個交易日**的逐筆資料一次抓回來，重採樣成幾分鐘一根是後端本機運算，不影響抓幾次/抓多少——這代表「兩個時間軸共用同一份抓回來的原始逐筆資料，各自本機重採樣」在技術上完全合理，不是勉強湊出來的優化：

```python
def backfill_via_shioaji(
    product_code: str, *, lookback_days: int = 2, interval_minutes_list: tuple[int, ...] = (60,)
) -> dict[int, int]:
    """
    每個 interval_minutes 各自找自己的缺口，合併成一組要查詢的日期（去重),
    避免同一天的逐筆資料被抓兩次。同一次抓回來的逐筆資料分別拿去補齊各個
    時間軸各自的 store。回傳 {interval_minutes: 實際補進去的根數}。
    """
    available = shioaji_available()
    if not available:
        _append_audit(f"product={product_code}\tskip=沒有可用的 SJ_API_KEY/SJ_SEC_KEY")
        return {m: 0 for m in interval_minutes_list}

    missing_by_interval = {
        m: find_missing_bars(product_code, lookback_days=lookback_days, interval_minutes=m)
        for m in interval_minutes_list
    }
    all_query_dates = sorted({
        query_date
        for missing in missing_by_interval.values()
        for query_date, _ in missing
    })
    if not all_query_dates:
        _append_audit(f"product={product_code}\tskip=沒有偵測到缺口")
        return {m: 0 for m in interval_minutes_list}

    # ...登入、取得合約（跟現有邏輯一樣，不因為多時間軸而改變)...

    added_by_interval = {m: 0 for m in interval_minutes_list}
    for query_date in all_query_dates:
        # ...api.ticks(contract=contract, date=query_date) 抓一次...
        # df 抓回來後，只對「這天真的缺」的時間軸做重採樣+寫入：
        for m in interval_minutes_list:
            if query_date not in {qd for qd, _ in missing_by_interval[m]}:
                continue  # 這個時間軸這天沒缺，不用重複補
            bars = _resample_ticks_to_bars(df, interval_minutes=m)
            if not bars:
                continue
            added = get_kline_store(product_code, interval_minutes=m).merge_missing_bars(bars)
            added_by_interval[m] += added
    return added_by_interval
```

回傳型別從單一 `int` 改成 `dict[int, int]`（每個時間軸各自補了幾根),原本唯一的呼叫端 `backend/api.py::repair_kline()`（60分鐘專用的手動熱修復端點,不在這次範圍內，維持只處理60分鐘)要跟著小改：`added = backfill_via_shioaji(prod, lookback_days=1)` 改成 `added = backfill_via_shioaji(prod, lookback_days=1).get(60, 0)`，行為不變，只是配合新的回傳型別。

## 二、`live_service.py` 觸發

**不需要新增獨立的節流計時變數**——既然改成一次呼叫處理兩個時間軸、共用同一個節流節奏就夠了，比原本設計的「兩條各自獨立節流」更簡單。在 `poll_once()` 裡，把現有呼叫：
```python
added = await asyncio.to_thread(backfill_via_shioaji, quote_product)
if added:
    logger.info("Shioaji 定期補缺：%s 新增 %d 根", quote_product, added)
```
改成：
```python
added_by_interval = await asyncio.to_thread(
    backfill_via_shioaji, quote_product, interval_minutes_list=(60, 15)
)
for interval_minutes, added in added_by_interval.items():
    if added:
        logger.info("Shioaji 定期補缺(%d分鐘)：%s 新增 %d 根", interval_minutes, quote_product, added)
```

**已確認的取捨**：這段邏輯無條件執行（不管死叉策略目前是否武裝),跟既有行為一致。穩態下（沒有缺口)兩個時間軸的 `find_missing_bars` 都回傳空清單，`backfill_via_shioaji` 在呼叫 Shioaji API 之前就提前 return，所以沒有缺口時只是兩次便宜的本機 K 棒掃描，不會真的連線 Shioaji。只有真的偵測到缺口（例如剛換約)才會觸發真正的 Shioaji 連線查詢，且同一天的缺口只會真的打一次 `ticks()`，不會因為兩個時間軸各自缺同一天而抓兩次。

**流量/次數查證**（設計時查了官方文件確認,不是憑印象):`ticks()` 盤中上限10次、所有查詢型行情函式合計10秒50次，限制綁在帳號（person_id),超過的話先暫停1分鐘、當天一直超過會停用帳號+IP；另外每日流量上限依近30日 Shioaji 帳號實際成交量分級，近30日0口成交（這個專案的 Shioaji 帳號目前只當備援資料源,沒有真的下單,應該落在這一級)是每日500MB，開盤日早上8:00重置，超過的話查詢回傳空值（不是停權)。這次回補用途只抓「重開機/換約」這種短期缺口（`lookback_days=2` 封頂),用 TMFR1 全量252MB/243天換算大約每天1MB，一次回補（通常橫跨1~3個查詢日)大概1~3MB，遠低於最保守的500MB/日,不會踩到任何一種限制。

## 三、測試計畫

- `_expected_day_session_labels(day, interval_minutes=15)`/`_expected_night_session_labels(day, interval_minutes=15)` 分別產生 20/56 個標籤，時間點正確；`interval_minutes=60`（預設)跟修改前行為一致（既有測試如果有的話要維持全過，沒有的話補一個涵蓋現況的測試）。
- `find_missing_bars` 用 `interval_minutes=15` 呼叫時，讀寫的是 `get_kline_store(product_code, interval_minutes=15)`（mock 驗證,不用真的連線 Shioaji，比照現有測試套件裡對 Shioaji 相關函式的 mock 手法)。
- `backfill_via_shioaji(interval_minutes_list=(60, 15))`：兩個時間軸都缺同一天時，只會呼叫一次 `api.ticks()`（mock 驗證呼叫次數),但兩個時間軸各自的 store 都有補到資料；只有一個時間軸缺某天時，該天抓回來的逐筆資料不會被拿去補另一個沒缺的時間軸。
- `repair_kline()` 端點在新的 `dict` 回傳型別下行為不變（回傳的 `added` 數字對應 60 分鐘）。
- `live_service.py`：`poll_once()` 只用一個節流計時器就能同時觸發兩個時間軸的回補檢查。

## 四、明確不在範圍內

- 不改變「沒有設定 SJ_API_KEY/SJ_SEC_KEY 時退回乾等」的既有行為——這份文件只是讓「有設定金鑰時」的補缺範圍擴大到15分鐘，沒設定金鑰的使用者體驗不變。
- 不處理大台/小台的 Shioaji 回補（同 Phase 1/2 理由，這幾個策略目前都只做 TMF）。
