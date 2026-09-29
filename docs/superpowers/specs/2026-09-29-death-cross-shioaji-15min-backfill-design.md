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

`_session_query_dates(day, interval_minutes=60)`、`find_missing_bars(product_code, *, lookback_days=2, interval_minutes=60)`、`_resample_ticks_to_bars(df, interval_minutes=60)`、`backfill_via_shioaji(product_code, *, lookback_days=2, interval_minutes=60)` 都往下多加同一個參數並繼續傳遞。`find_missing_bars`/`backfill_via_shioaji` 內部原本呼叫 `get_store(product_code)` 的地方，改成 `get_kline_store(product_code, interval_minutes=interval_minutes)`（`get_store` 這個名字在這支檔案裡不再需要，import 只留 `get_kline_store`)。

`backfill_via_shioaji` 內部查詢 Shioaji 合約用的是 `api.Contracts.Futures.TMF.TMFR1`（近月連續代碼),跟 `interval_minutes` 無關——這代表不管幫哪個時間軸回補，查詢 Shioaji 這一步完全不用改，只有「查回來的逐筆資料要分桶成幾分鐘一根、寫進哪個 store」這兩件事需要依 `interval_minutes` 決定，這也是這個泛化能做到「風險低、改動集中」的原因。

## 二、`live_service.py` 觸發

新增一個獨立的節流計時變數（不能共用 60 分鐘那個，兩條要各自獨立的 5 分鐘節奏）：
```python
_last_shioaji_backfill_attempt_15min: float = 0.0
```

在 `poll_once()` 裡，緊接著現有 60 分鐘那段之後，新增幾乎一樣的邏輯：
```python
if not _is_worker_mode() and _should_attempt_periodic_shioaji_backfill(now, _last_shioaji_backfill_attempt_15min):
    _last_shioaji_backfill_attempt_15min = now
    try:
        from .shioaji_backfill import backfill_via_shioaji

        added = await asyncio.to_thread(backfill_via_shioaji, quote_product, interval_minutes=15)
        if added:
            logger.info("Shioaji 定期補缺(15分鐘)：%s 新增 %d 根", quote_product, added)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Shioaji 15分鐘定期補缺失敗（不影響主流程): %s", exc)
```

**已確認的取捨**：這段邏輯無條件執行（不管死叉策略目前是否武裝),跟 60 分鐘的既有行為一致。穩態下（沒有缺口)`find_missing_bars` 直接回傳空清單，`backfill_via_shioaji` 在呼叫 Shioaji API 之前就提前 return（見現有 `backfill_via_shioaji` 的 `if not missing: return 0` 這一步），所以沒有缺口時只是一次便宜的本機 K 棒掃描，不會真的連線 Shioaji、不會產生額外的外部 API 負擔。只有真的偵測到缺口（例如剛換約)才會觸發一次真正的 Shioaji 連線查詢。

## 三、測試計畫

- `_expected_day_session_labels(day, interval_minutes=15)`/`_expected_night_session_labels(day, interval_minutes=15)` 分別產生 20/56 個標籤，時間點正確；`interval_minutes=60`（預設)跟修改前行為一致（既有測試如果有的話要維持全過，沒有的話補一個涵蓋現況的測試）。
- `find_missing_bars`/`backfill_via_shioaji` 用 `interval_minutes=15` 呼叫時，讀寫的是 `get_kline_store(product_code, interval_minutes=15)`（mock 驗證,不用真的連線 Shioaji，比照現有測試套件裡對 Shioaji 相關函式的 mock 手法)。
- `live_service.py`：兩個節流計時器（60分鐘/15分鐘）各自獨立運作，觸發其中一個不影響另一個的節奏。

## 四、明確不在範圍內

- 不改變「沒有設定 SJ_API_KEY/SJ_SEC_KEY 時退回乾等」的既有行為——這份文件只是讓「有設定金鑰時」的補缺範圍擴大到15分鐘，沒設定金鑰的使用者體驗不變。
- 不處理大台/小台的 Shioaji 回補（同 Phase 1/2 理由，這幾個策略目前都只做 TMF）。
