# Pullback 策略 A/B 測試結果

## 實驗設定
- 商品：MTX, TX
- pullback baseline：60MA + 20MA 趨勢對齊，回測/回彈 20MA 進場，SL=150 點，TP=300 點。
- 成交規則：沿用既有回測引擎的下一根開盤進場、同棒先停損、跳空以開盤價成交、含雙邊成本與稅。
- ATR 變體：訊號棒收盤時計算 ATR14，下一根開盤進場，停損距離 = ATR14 × 倍數，TP 維持 300 點。
- `Disable immediate_reverse` 變體：保留同一組 pullback regime / 20MA 觸價邏輯，但 opposite arrow 不再即時平倉反手；這個版本改以『每個合格 setup 都可發訊號』重建 signal frame，避免單純把 flag 清掉後仍被 alternating-state 汙染。

## 變體清單

- **A. Baseline**：原始 pullback：SL=150、TP=300，保留 opposite-arrow immediate_reverse。
- **B1. Tight SL -30%**：固定停損縮至 105 點（較 baseline 150 點縮小 30%），TP 維持 300 點。
- **B2. Tight SL -50%**：固定停損縮至 75 點（較 baseline 150 點縮小 50%），TP 維持 300 點。
- **C1. Wide SL +50%**：固定停損放寬至 225 點（較 baseline 增加 50%），TP 維持 300 點。
- **C2. Wide SL +100%**：固定停損放寬至 300 點（較 baseline 增加 100%），TP 維持 300 點。
- **D1. ATR14 × 1.5 SL**：訊號棒 ATR14 × 1.5 作為停損距離，TP 維持 baseline 300 點。
- **D2. ATR14 × 2.5 SL**：訊號棒 ATR14 × 2.5 作為停損距離，TP 維持 baseline 300 點。
- **E. Disable immediate_reverse**：停用 opposite-arrow 即時平倉反手；持倉改為只靠自身 SL/TP 出場。

## 各商品結果

### MTX
- 資料區間：2026-05-22T16:00:00 ~ 2026-07-03T13:45:00（551 根 60 分 K）

| 變體 | 交易數 | 勝率 | Profit Factor | 總淨損益(NTD) | Total Return (%) | Sharpe Ratio | Max DD (%) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A. Baseline | 5 | 40.0% | 1.30 | 6,794 | 0.30 | 0.73 | 0.67 |
| B1. Tight SL -30% | 5 | 20.0% | 0.69 | -6,705 | -0.29 | -0.98 | 0.47 |
| B2. Tight SL -50% | 5 | 20.0% | 0.95 | -705 | -0.03 | -0.11 | 0.34 |
| C1. Wide SL +50% | 5 | 60.0% | 1.96 | 21,794 | 0.96 | 2.00 | 0.50 |
| C2. Wide SL +100% | 5 | 80.0% | 3.93 | 44,294 | 1.95 | 4.36 | 0.67 |
| D1. ATR14 × 1.5 SL | 5 | 100.0% | ∞ | 87,495 | 3.85 | 19.57 | 0.00 |
| D2. ATR14 × 2.5 SL | 5 | 100.0% | ∞ | 87,495 | 3.85 | 19.57 | 0.00 |
| E. Disable immediate_reverse | 16 | 50.0% | 2.59 | 97,345 | 4.28 | 4.20 | 1.01 |

### TX
- 資料區間：2026-05-22T16:00:00 ~ 2026-07-03T13:45:00（551 根 60 分 K）

| 變體 | 交易數 | 勝率 | Profit Factor | 總淨損益(NTD) | Total Return (%) | Sharpe Ratio | Max DD (%) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A. Baseline | 6 | 33.3% | 0.98 | -2,494 | -0.03 | -0.06 | 0.67 |
| B1. Tight SL -30% | 6 | 16.7% | 0.56 | -47,492 | -0.52 | -1.73 | 0.71 |
| B2. Tight SL -50% | 6 | 16.7% | 0.77 | -17,492 | -0.19 | -0.69 | 0.51 |
| C1. Wide SL +50% | 6 | 50.0% | 1.31 | 42,503 | 0.47 | 0.89 | 0.50 |
| C2. Wide SL +100% | 6 | 66.7% | 1.97 | 117,505 | 1.29 | 2.29 | 0.66 |
| D1. ATR14 × 1.5 SL | 6 | 83.3% | 3.18 | 240,529 | 2.64 | 3.78 | 1.21 |
| D2. ATR14 × 2.5 SL | 6 | 83.3% | 1.91 | 167,342 | 1.84 | 1.91 | 2.02 |
| E. Disable immediate_reverse | 17 | 47.1% | 2.32 | 361,158 | 3.97 | 3.83 | 1.00 |

## 重點觀察

### MTX
- **pullback 最佳 Total Return 變體**：E. Disable immediate_reverse，Total Return 4.28%、Sharpe 4.20、總淨損益 97,345 NTD。
- **pullback 最差變體**：B1. Tight SL -30%，Total Return -0.29%、Sharpe -0.98。
- **停用 immediate_reverse 的影響**：baseline 5 筆 / 0.30%，停用後 16 筆 / 4.28%；baseline immediate_reverse 出場次數 = 0，表示改善主因不是『少了被強迫反手的實際出場』，而是 baseline 為了支援 reverse 所採用的 alternating-only signal contract 壓掉了許多同向 pullback setup。
- **pullback baseline vs breakout baseline**：pullback baseline 勝率 40.0%、Total Return 0.30%、Sharpe 0.73；breakout baseline 勝率 20.8%、Total Return -1.09%、Sharpe -1.10。

### TX
- **pullback 最佳 Total Return 變體**：E. Disable immediate_reverse，Total Return 3.97%、Sharpe 3.83、總淨損益 361,158 NTD。
- **pullback 最差變體**：B1. Tight SL -30%，Total Return -0.52%、Sharpe -1.73。
- **停用 immediate_reverse 的影響**：baseline 6 筆 / -0.03%，停用後 17 筆 / 3.97%；baseline immediate_reverse 出場次數 = 0，表示改善主因不是『少了被強迫反手的實際出場』，而是 baseline 為了支援 reverse 所採用的 alternating-only signal contract 壓掉了許多同向 pullback setup。
- **pullback baseline vs breakout baseline**：pullback baseline 勝率 33.3%、Total Return -0.03%、Sharpe -0.06；breakout baseline 勝率 20.8%、Total Return -0.97%、Sharpe -0.99。

## 多重檢定 / Deflated Sharpe Ratio 脈絡

- 先前 breakout 研究已累積 **12** 個非 baseline 變體，純運氣 Sharpe 門檻約 **4.18**（見 `proper_evaluation_with_dsr.md`）。
- 本次 pullback 又新增 **7** 個非 baseline 變體，因此整個專案的累積試驗次數已變成 **19 = 12 + 7**。
- 若先只固定沿用舊 breakout 的 sigma_SR=1.874，單純把 N 從 12 擴到 19，則『純運氣可抽到的最高 Sharpe』門檻大約會升到 **4.55**。
- 若把本次 pullback 非 baseline 變體也一起納入樣本內 Sharpe 離散度估計，combined sigma_SR 約 **5.391**，對應 luck-threshold 約 **13.08**。
- 換句話說：本輪若只有小幅優於 baseline、但 Sharpe 仍遠低於上述 luck-threshold，不能把它當成穩健 alpha，頂多視為下一輪樣本外驗證候選。

## 結論

- pullback baseline 仍明顯優於 breakout baseline 的核心特徵，是**交易更少、勝率更高**；但樣本只有 5~6 筆，統計信心仍然很弱。
- 本輪重點不是『硬把 pullback 優化到顯著穩健』，而是檢查：改停損寬度、改 ATR 自適應停損、或關閉 immediate_reverse，是否能在這份短樣本上**清楚打敗 baseline pullback**。
- **樣本內**確實有多個變體優於 baseline：緊停損明顯最差；放寬停損、ATR 停損與停用 immediate_reverse 都比 baseline 好，且 `Disable immediate_reverse` 在 MTX/TX 都同步給出最高 Total Return。
- 但若把『本專案其實已經累積測過 19 個非 baseline 變體』算進去，沒有任何一個 pullback 變體能在 **兩個商品都穩定且明顯** 超越更新後的 luck-threshold，因此**目前不能宣稱已找到穩健優化版**；最合理的結論仍是：`Disable immediate_reverse` 與較寬/ATR 停損值得拿去做更長期樣本外驗證。
