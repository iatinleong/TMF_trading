# Pullback 訊號重複同向觸價 bug 修復

## 問題機制
- 原本 `backend/signals.py` 的 `_apply_signal_columns()` 只記錄「上一次發出的方向」，所以在 pullback 策略裡，只要前一次發過 `long`，後面所有新的 `long` 觸價都會被壓掉，直到先出現 `short` 才會重新放行。
- 這個 contract 對 breakout crossover 合理，但對 pullback 不合理：趨勢沒有反轉時，價格之後再次回測 20MA，本來就應該是新的可交易事件。
- 回測引擎本身已經只會在 `position is None` 時開新倉，所以不需要在訊號層用「同向必須交替」來避免重複進場。

## 修復方式
- 保留共享 helper，新增 `suppress_repeat_same_direction` 參數。
- `generate_breakout_signals()` 維持預設 `True`，因此 breakout 行為完全不變。
- `generate_pullback_signals()` 改為傳入 `False`：同向 pullback 只要是新的 edge-trigger 觸價事件，就可以再次發出訊號。
- `immediate_reverse` 邏輯完全保留：只要新訊號方向與上一個已發出訊號相反，仍然標記為 `True`，讓引擎照舊在下一根開盤做平倉反手。

## 單元測試
- 修改前既有 suite：`7 passed`
- 修改後完整 suite：`9 passed`
- 新增 2 個 regression tests：
  - 驗證 pullback 兩次獨立的同向 20MA 觸價，現在都會各自形成一筆交易。
  - 驗證 pullback 出現反向觸價時，`immediate_reverse` 仍然正確標記。

## Pullback baseline（完整資料，修復前後）

| 商品 | 版本 | 交易數 | 勝率 | Profit Factor | 總淨損益(NTD) | Total Return (%) |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| MTX | 修復前 | 5 | 40.00% | 1.30 | 6,794 | 0.30% |
| MTX | 修復後 | 16 | 50.00% | 2.59 | 97,345 | 4.28% |
| TX | 修復前 | 6 | 33.33% | 0.98 | -2,494 | -0.03% |
| TX | 修復後 | 17 | 47.06% | 2.32 | 361,158 | 3.97% |

## Breakout baseline 驗證（必須完全不變）

| 商品 | 修復前交易數 | 修復後交易數 | 修復前總淨損益 | 修復後總淨損益 | 結論 |
| --- | ---: | ---: | ---: | ---: | --- |
| MTX | 24 | 24 | -24,785 | -24,785 | 完全一致 |
| TX | 24 | 24 | -88,541 | -88,541 | 完全一致 |

- Breakout 是否維持完全一致：**是**

## 結論
- 這次修復只改 pullback 的訊號發射 contract，沒有改 backtest engine 的持倉判斷、SL/TP 或 immediate_reverse 執行邏輯。
- 修復後的 pullback baseline 交易數明顯回到合理水準；而 breakout baseline 的交易數與績效完全一致，確認沒有被副作用影響。
