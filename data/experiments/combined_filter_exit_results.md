# C1 進場濾網 + D2 ATR 出場：組合交互作用測試

## 1. 測試目的
- 檢查先前分開表現最佳的 **C1（進場最小分離 0.05%）** 與 **D2（ATR14 × 2.5 停損 / TP500）**，
  在同一策略內同時啟用後，是否出現互補、互相抵消，或幾乎沒有交互作用。
- 比較 4 個版本：baseline、C1 單獨、D2 單獨、COMBINED（C1 + D2）。
- 指標沿用 `proper_evaluation_with_dsr.py`：交易數、勝率、Profit Factor、總淨損益、
  Total Return %、Sharpe Ratio（逐筆交易年化）、Max Drawdown %。

## 2. 方法與一致性說明
- 進場濾網直接沿用 `proper_evaluation_with_dsr.py::_breakout_signals_with_filter()` 的 C1 寫法。
- ATR 停損直接沿用 `proper_evaluation_with_dsr.py::_atr14()` 與 `_run_exit_variant()` 的 per-trade sliced rerun 模式。
- Total Return、Sharpe、Max Drawdown 與 Sharpe 不穩定判定，也全部沿用同一份正式評估邏輯。
- **資本基準假設**：`assumed_capital = 平均 close 價格 × point_value`；因此 Total Return % 是相對於這個假設資本的規模化結果，不是實際保證金報酬率。

## 3. 多重檢定門檻（Prado / DSR 概念）
- 先前正式報告使用 **N=12**，pure-luck Expected Max Sharpe 約 **4.178**。
- 本次新增的真正『新試法』只有 **COMBINED 1 個新變體**，因此理應把 trial count 更新為 **N=13**。
- 納入 COMBINED 後重新估計，pure-luck Expected Max Sharpe 約 **4.378**。

## 4. MTX 結果
| 變體 | 交易數 | 勝率 | Profit Factor | 總淨損益(NTD) | Total Return % | Sharpe Ratio | Max Drawdown % |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Baseline（無濾網、預設 SL/TP） | 24 | 20.8% | 0.83 | -24,785 | -1.09% | -1.10 | 2.52% |
| C1 單獨（進場最小分離 0.05%） | 18 | 22.2% | 0.90 | -11,431 | -0.50% | -0.57 | 2.52% |
| D2 單獨（ATR14 × 2.5 停損 / TP500） | 24 | 62.5% | 1.06 | 20,956 | 0.92% | 0.38 | 4.96% |
| COMBINED（C1 + D2） | 18 | 61.1% | 1.09 | 22,413 | 0.99% | 0.48 | 4.96% |

## 4. TX 結果
| 變體 | 交易數 | 勝率 | Profit Factor | 總淨損益(NTD) | Total Return % | Sharpe Ratio | Max Drawdown % |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Baseline（無濾網、預設 SL/TP） | 24 | 20.8% | 0.85 | -88,541 | -0.97% | -0.99 | 2.44% |
| C1 單獨（進場最小分離 0.05%） | 18 | 22.2% | 0.92 | -36,024 | -0.40% | -0.45 | 2.44% |
| D2 單獨（ATR14 × 2.5 停損 / TP500） | 24 | 62.5% | 1.05 | 77,589 | 0.85% | 0.35 | 5.02% |
| COMBINED（C1 + D2） | 18 | 61.1% | 1.09 | 86,504 | 0.95% | 0.46 | 5.02% |

## 5. 重點發現

- **MTX**：COMBINED 交易數 18，優於 D2 單獨；優於 C1 單獨；優於 baseline。
- **MTX**：combined Sharpe 0.48 明顯低於更新後 N=13 的 luck threshold 4.38。
- **TX**：COMBINED 交易數 18，優於 D2 單獨；優於 C1 單獨；優於 baseline。
- **TX**：combined Sharpe 0.46 明顯低於更新後 N=13 的 luck threshold 4.38。

## 6. 整體結論
- **整體判讀**：COMBINED 在兩個商品都優於 D2 單獨，代表 C1 與 D2 有正向互補。
- **是否超過 Prado 運氣門檻**：沒有。即使使用更新後的 N=13 門檻，COMBINED 的 Sharpe 仍遠低於 pure-luck threshold，不能視為已證明的 alpha。
- **交易數檢查**：COMBINED 的交易數應明顯低於 baseline 24 筆；本次實測結果已列於上表，可直接檢查是否符合『濾網 + ATR 應壓低出手數』的直覺。
- **建議**：若仍想繼續追蹤 COMBINED，下一步應以更長期間與樣本外資料重跑，而不是只依這 30 天小樣本下結論。