# 死叉做空策略（15分鐘K，含MA20斜率過濾）— Phase 1 回測驗證 設計文件

## 背景與目標

新增一個只做空的策略：15分鐘K棒上，MA5 由上往下死叉穿越 MA20，且同時 MA20 本身斜率向下（排除盤整假訊號），才觸發做空訊號。停損停利各 150 點，用單一 OCO（二擇一）保護單。

**這份設計只做到「用歷史資料回測驗證邏輯是否有效」，不接上即時交易。** 即時交易需要额外新建一條 15 分鐘 tick→K棒聚合管線（詳見下方「明確不在範圍內」），風險與工作量都跟這次的回測驗證分開處理，等 Phase 1 驗證出的數字有意義後再另外規劃。

## 現有架構（已核對，直接重用）

專案既有的 4 個策略（突破多/空、回測多/空）共用同一套「訊號函式 → 方向限定回測引擎」架構：

- `backend/signals.py`：每個策略一個 `generate_*_signals(df)` 函式，輸入含 `ma_fast`/`ma_mid`（必要時 `ma_slow`）的 K 棒 DataFrame，輸出加上 `signal`/`signal_color`/`signal_bar_close_time` 欄位。訊號在**本根K棒收盤確認**，執行在**下一根K棒開盤**——這個合約寫死在檔頭註解，所有訊號函式必須遵守。
- `backend/backtest_engine.py::run_direction_limited_backtest()`：吃已經訊號化的 DataFrame，模擬方向限定進出場，支援 `direction_limit`、點數 SL/TP、NTD/%風控保險、`reverse_signal_exit_enabled`（反向訊號是否觸發出場）等開關，跟實盤 `backend/strategy_service.py::_tick_one()` 的邏輯是對齊的（4 道防護網概念一致)。

死叉做空策略只需要新增一個訊號函式、把新策略名稱接進既有白名單，**不需要重新設計狀態機或回測流程**。

## 訊號邏輯

新增 `generate_death_cross_signals(df)`，放在 `backend/signals.py`，跟現有兩個訊號函式同一種寫法（共用 `_apply_signal_columns`)：

```python
def generate_death_cross_signals(df: pd.DataFrame) -> pd.DataFrame:
    _validate_columns(df, ["ma_fast", "ma_mid"])

    previous_fast = df["ma_fast"].shift(1)
    previous_mid = df["ma_mid"].shift(1)

    cross_down = previous_fast.ge(previous_mid) & df["ma_fast"].lt(df["ma_mid"])
    slope_down = df["ma_mid"].lt(previous_mid)

    short_mask = cross_down & slope_down
    long_mask = pd.Series(False, index=df.index)  # 只做空，金叉不產生任何訊號

    return _apply_signal_columns(
        df,
        long_mask=long_mask,
        short_mask=short_mask,
        suppress_repeat_same_direction=False,  # 見下方說明：不能用預設值 True
    )
```

- **`suppress_repeat_same_direction` 必須明確傳 `False`，不能用預設值 `True`**（這是本文件第一版寫的錯誤，已用實際程式碼跑過驗證修正）。`_apply_signal_columns` 的 `suppress_repeat_same_direction=True` 設計原意是給 breakout 這種多空交替策略用：靠「跟上一次發出的訊號方向不同」判斷是否要重置。但這個策略 `long_mask` 永遠是 `False`，`last_emitted_signal` 一旦在第一次死叉後變成 `"short"`，之後就再也不會被重設成別的值——用預設值會導致**整個回測只有第一次死叉觸發訊號，之後所有死叉都被靜默吃掉**（已實測驗證：3 次獨立死叉事件，預設值只讓第 1 次通過）。
  - 改成 `False` 是安全的：`cross_down` 是 edge-triggered 條件（用 `.shift(1)` 比較上一根），同一次穿越事件不可能連續兩根都成立，數學上不會重複觸發同一次死叉；「訊號重複但已有持倉」這件事本來就由回測引擎的 `if position is None` 進場判斷擋著，不需要靠這個旗標防護。
- 只有 `short_mask`，沒有 long 訊號——回測引擎的 `direction_limit="short"` 本來就會擋掉任何非 short 訊號的進場，這裡多一層「金叉訊號根本不存在」是雙重保險。

## 持倉期間金叉的處理（已與使用者確認）

**金叉忽略，不提前出場，只靠 150 點 TP/SL 平倉。** 對應 `run_direction_limited_backtest(..., reverse_signal_exit_enabled=False)`。

這點原始需求只寫了「金叉不做多、不反手」，沒有明講金叉是否要提前平倉既有空單——跟另一個 AI（Gemini）討論時它預設成「金叉不平倉」並稱之為已確定的「2A」，但這是它自己的假設，不是使用者先前跟本文件作者（Claude）確認過的內容。已經直接跟使用者重新確認過，結論就是上面這條：**金叉忽略、嚴格只靠 150 點 TP/SL 出場**。

## 15 分鐘K棒重採樣

`backend/indicators.py::resample_to_60min()` 目前把 60 分鐘桶寫死在函式內部（日盤 08:45-13:45 共 300 分鐘、夜盤 15:00-次日05:00 共 840 分鐘，兩者都恰好被 15 整除，不會有零頭桶問題）。

做法：把日夜盤切分邏輯抽出來，新增參數化版本：

```python
def resample_to_nmin(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    ...  # 原本 resample_to_60min 的邏輯，桶寬改用 minutes 參數

def resample_to_60min(df: pd.DataFrame) -> pd.DataFrame:
    return resample_to_nmin(df, minutes=60)
```

`resample_to_60min` 對外行為完全不變（既有測試、即時系統呼叫端都不用動)，這是唯一會修改這個共用函式的地方，用「抽出參數、原函式變薄包裝」把改動範圍鎖到最小。這個函式是純 pandas 批次運算，跟即時 tick 處理路徑（`backend/kline_engine.py`，這個 session 稍早修過兩次事故的地方）完全無關。

## 接進回測引擎

`backend/backtest_engine.py` 需要三處新增 `"death_cross"` 分支（已核對現有程式碼、確認這三處都是必要的，缺一會直接 `ValueError` 擋下）：

1. `StrategyName = Literal["breakout", "pullback", "death_cross"]`
2. `BacktestEngine.__init__` 的白名單 `if strategy not in {"breakout", "pullback", "death_cross"}: raise ValueError(...)`
3. `_resolve_risk_parameters()`：`death_cross` 分支回傳 `(150.0, 150.0)` 作為預設值（呼叫端仍可用明確參數覆寫，跟現有兩個策略行為一致）
4. `_generate_signals()`：`death_cross` 分支呼叫 `generate_death_cross_signals`

呼叫時額外注意：`add_moving_averages(df, fast, mid, slow)` 三個參數都必填、無預設值，即使死叉策略只用 `fast=5`/`mid=20`，呼叫時仍要多傳 `slow=60`（值不會被用到，純粹滿足函式簽名，不改動共用函式簽名本身，風險最低）。

## 資料流（回測）

```
data/raw_tick/TMFR1/*.parquet（逐筆)
  → resample_to_nmin(minutes=15)
  → add_moving_averages(fast=5, mid=20, slow=60)
  → generate_death_cross_signals
  → run_direction_limited_backtest(
        strategy="death_cross",
        direction_limit="short",
        stop_loss_points=150.0,
        take_profit_points=150.0,
        reverse_signal_exit_enabled=False,
        ...
    )
```

## 商品範圍

程式碼本身商品無關（沿用 `CONTRACT_SPECS`/現有商品代碼解析),大台/小台一樣能跑。但深度回測資料目前只有微台（`data/raw_tick/TMFR1/`，2024-07-29 至今）夠長；大台/小台只有近 30 個交易日的短窗資料（`data/TX_60min_real.csv`/`MTX_60min_real.csv`，且是 60 分鐘、不是 15 分鐘）。

**這次先用 TMF 完整跑一次驗證。** 大台/小台要有意義的長期回測結果，需要另外用 Shioaji 回補逐筆資料——這是獨立工作量，不在這次範圍內。

## 測試計畫

- `tests/test_signals.py`：死叉但斜率向上不觸發／死叉+斜率向下觸發／連續死叉只觸發一次／全程不會出現 long 訊號／**兩次時間上分開的獨立死叉事件都要各自觸發訊號**（防止 `suppress_repeat_same_direction` 設錯導致回測全程只成交一筆這個已經抓到過的 bug 回歸)。
- `tests/test_indicators.py`：`resample_to_nmin(minutes=15)` 日盤/夜盤分別產生 20/56 根；`resample_to_60min()` 薄包裝跟改動前行為一致（既有測試應維持全過）。
- `tests/test_backtest_engine.py`：`"death_cross"` 能跑完整個 `run_direction_limited_backtest`；持倉中出現金叉時position 不變、只靠 SL/TP 出場的行為驗證。
- 最後用 TMFR1 全量歷史資料實際跑一次，產出勝率/期望值/最大回撤。

## 明確不在這次範圍內（Phase 2，之後另外規劃）

- 即時 15 分鐘 tick→K棒聚合管線（`backend/kline_engine.py` 目前整條路徑寫死 60 分鐘,新建平行的 15 分鐘聚合器,不修改既有 60 分鐘路徑)。
- 接進 `backend/strategy_service.py` 的即時武裝/4道防護網、Dashboard 開關、Supabase `user_strategy_configs` 綁定。
- 大台/小台的長期歷史逐筆資料回補（Shioaji)。
