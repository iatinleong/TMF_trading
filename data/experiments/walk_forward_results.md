# Walk-Forward Validation（60% IS / 40% OOS）

## 1. 方法說明
- 僅使用目前這份約 30 個交易日、551 根 60 分 K 的資料。
- 先按時間排序，再切成：前 60% 為 in-sample（IS），後 40% 為 out-of-sample（OOS）。
- 每個視窗**獨立**計算 MA / ATR 並各自回測，避免把 OOS 資料洩漏到 IS 結果。
- 因為每個視窗都重新暖機指標，IS 與 OOS 的交易數相加不一定會等於整段資料一次跑完的交易數，這是刻意接受的無洩漏代價。
- 納入變體：Baseline breakout, C1 進場濾網, D2 出場機制, C1 + D2 組合。
- 已偵測到 combined_filter_exit_test.py，因此本次納入 C1+D2 組合。
- 指標：交易數、勝率、Profit Factor、Total Return %、Sharpe Ratio（沿用 `proper_evaluation_with_dsr.py` 的不穩定判定）、Max Drawdown%。

## 2. 分割區間

| 商品 | 總 K 棒 | IS K 棒 | IS 區間 | OOS K 棒 | OOS 區間 |
| --- | ---: | ---: | --- | ---: | --- |
| MTX | 551 | 330 | 2026-05-22 16:00:00 ~ 2026-06-16 22:00:00 | 221 | 2026-06-16 23:00:00 ~ 2026-07-03 13:45:00 |
| TX | 551 | 330 | 2026-05-22 16:00:00 ~ 2026-06-16 22:00:00 | 221 | 2026-06-16 23:00:00 ~ 2026-07-03 13:45:00 |

## 3. MTX：IS vs OOS

| 變體 | IS 交易數 | IS 勝率 | IS PF | IS Total Return | IS Sharpe | IS MaxDD | OOS 交易數 | OOS 勝率 | OOS PF | OOS Total Return | OOS Sharpe | OOS MaxDD | 判讀 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Baseline breakout | 15 | 20.0% | 0.55 | -1.91% | -3.62 | 2.56% | 8 | 25.0% | 1.08 | 0.17% | 0.42 | 0.99% | OOS 大致守住 |
| C1 進場濾網 | 13 | 23.1% | 0.66 | -1.23% | -2.35 | 2.56% | 4 | 25.0% | 1.08 | 0.08% | 0.28 | 0.99% | OOS 大致守住 |
| D2 出場機制 | 15 | 66.7% | 1.25 | 2.02% | 1.48 | 3.07% | 8 | 50.0% | 0.59 | -3.06% | -2.91 | 4.85% | 疑似過度擬合 |
| C1 + D2 組合 | 13 | 69.2% | 1.46 | 2.83% | 2.33 | 3.07% | 4 | 25.0% | 0.22 | -3.78% | -5.14 | 4.85% | 疑似過度擬合 |

重點判讀：
- **MTX / Baseline breakout**：OOS 大致守住。IS Total Return -1.91% / PF 0.55；OOS Total Return 0.17% / PF 1.08。OOS 沒有明顯崩潰，表現大致維持。
- **MTX / C1 進場濾網**：OOS 大致守住。IS Total Return -1.23% / PF 0.66；OOS Total Return 0.08% / PF 1.08。OOS 沒有明顯崩潰，表現大致維持。
- **MTX / D2 出場機制**：疑似過度擬合。IS Total Return 2.02% / PF 1.25；OOS Total Return -3.06% / PF 0.59。OOS 相比 IS 明顯惡化，符合典型 overfitting / data-snooping 訊號。
- **MTX / C1 + D2 組合**：疑似過度擬合。IS Total Return 2.83% / PF 1.46；OOS Total Return -3.78% / PF 0.22。OOS 相比 IS 明顯惡化，符合典型 overfitting / data-snooping 訊號。

## 4. TX：IS vs OOS

| 變體 | IS 交易數 | IS 勝率 | IS PF | IS Total Return | IS Sharpe | IS MaxDD | OOS 交易數 | OOS 勝率 | OOS PF | OOS Total Return | OOS Sharpe | OOS MaxDD | 判讀 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Baseline breakout | 15 | 20.0% | 0.56 | -1.84% | -3.51 | 2.47% | 8 | 25.0% | 1.09 | 0.18% | 0.46 | 0.98% | OOS 大致守住 |
| C1 進場濾網 | 13 | 23.1% | 0.67 | -1.16% | -2.23 | 2.47% | 4 | 25.0% | 1.09 | 0.09% | 0.30 | 0.98% | OOS 大致守住 |
| D2 出場機制 | 15 | 66.7% | 1.25 | 2.02% | 1.48 | 3.07% | 8 | 50.0% | 0.58 | -3.15% | -3.00 | 4.91% | 疑似過度擬合 |
| C1 + D2 組合 | 13 | 69.2% | 1.46 | 2.83% | 2.32 | 3.07% | 4 | 25.0% | 0.22 | -3.84% | -5.26 | 4.91% | 疑似過度擬合 |

重點判讀：
- **TX / Baseline breakout**：OOS 大致守住。IS Total Return -1.84% / PF 0.56；OOS Total Return 0.18% / PF 1.09。OOS 沒有明顯崩潰，表現大致維持。
- **TX / C1 進場濾網**：OOS 大致守住。IS Total Return -1.16% / PF 0.67；OOS Total Return 0.09% / PF 1.09。OOS 沒有明顯崩潰，表現大致維持。
- **TX / D2 出場機制**：疑似過度擬合。IS Total Return 2.02% / PF 1.25；OOS Total Return -3.15% / PF 0.58。OOS 相比 IS 明顯惡化，符合典型 overfitting / data-snooping 訊號。
- **TX / C1 + D2 組合**：疑似過度擬合。IS Total Return 2.83% / PF 1.46；OOS Total Return -3.84% / PF 0.22。OOS 相比 IS 明顯惡化，符合典型 overfitting / data-snooping 訊號。

## 5. 核心發現
- 先前 purely in-sample 最亮眼的 **D2** 與 **C1+D2 組合**，在 MTX / TX 的 OOS 都同步出現 Total Return、PF、Sharpe 明顯反轉，屬於最值得警惕的 overfitting 訊號。
- **Baseline** 與 **C1 單獨** 在這次切法下至少沒有 OOS 崩潰，甚至出現小幅轉正；但由於 OOS 交易數只有 8 筆與 4 筆左右，這不能被解讀成「已證明有穩健 alpha」，頂多只能說目前沒有像 D2 那樣立即失真。

## 6. 結論
- 這次 walk-forward 的價值在於：**終於把先前所有 purely in-sample 找到的「較佳變體」拉到真正未參與調參的 OOS 區段檢查**，可直接觀察是否出現典型 overfitting 崩塌。
- 若某變體 OOS 明顯弱於 IS（例如 Total Return / PF / Sharpe 同步下滑），應優先視為資料探勘偏誤警訊，而不是把 IS 的漂亮數字當成可部署優勢。
- 但也必須誠實承認：整份資料只有約 30 天，OOS 只有後 40%（約 12 天、221 根 K），交易筆數非常少；因此這份報告**只足以示範方法論正確落地，完全不足以做統計上定論**。
- 真正要評估策略是否可用，下一步仍必須擴充到更長期、跨不同波動 regime 的 OOS / rolling walk-forward 歷史，再看候選變體是否能持續守住表現，否則不應投入真實資金。
