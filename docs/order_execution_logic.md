# 下單邏輯：進場、出場、停損停利

> 這份文件描述**目前程式碼實際的行為**（對照 `backend/strategy_service.py`、
> `backend/broker/capital_futures.py`），不是設計理想值。每個結論後面都標了
> 對應的程式位置，之後邏輯改了要記得回來同步更新。

## 一句話總結

**進場/出場一律是市價 FOK 單**（不是限價單）。**停損停利分兩層**：進場成交後
會嘗試在券商端掛「真正的智慧單」（STP=停損／MIT=停利）長期頂著；如果掛不成
（非 TMF 具體月份碼商品，或送單失敗），退回**軟停損停利**（後端自己每輪
輪詢比對點數，觸發時再送一張市價單平倉）。兩層不會同時搶著平倉。

## 1. 進場

訊號來源：4 個獨立方向策略之一（`breakout_long`/`breakout_short`/
`pullback_long`/`pullback_short`），偵測到收盤確認的訊號後呼叫：

```python
svc.place_order({
    "product_code": state.product_code,
    "side": "buy" 或 "sell",       # 依訊號方向
    "qty": state.qty,               # 預設 1 口（STRATEGY_QTY 環境變數可調）
    "price": "M",                   # 市價
    "trade_type": 2,                # FOK：全部成交否則取消
    "new_close": 0,                 # 新倉
})
```
（`backend/strategy_service.py` 第 932-941 行）

- **市價、FOK**，不是限價單，也不是 IOC/ROD。FOK 的意思是「要嘛立刻整口成交，
  要嘛整張作廢，不會留一部分掛在那邊等」。
- 進場成功後才會設定 `state.held_qty`/`held_direction`/`entry_price`，並接著
  嘗試掛真實的 STP/MIT 智慧單（見下面第 3 節）。

## 2. 出場（訊號出場，非停損停利觸發）

方向限定策略只做單邊，**出現反向訊號時只平倉出場，不會反手做另一邊**：

```python
svc.place_order({
    "product_code": state.product_code,
    "side": "sell" 或 "buy",        # 跟持倉方向相反
    "qty": state.held_qty,
    "price": "M",
    "trade_type": 2,                # 一樣是市價 FOK
    "new_close": 1,                 # 平倉
})
```
（`backend/strategy_service.py` 第 988-996 行）

成功平倉後會撤掉這筆倉位掛著的 STP/MIT 智慧單（如果有的話），避免留下孤兒單。

## 3. 智慧單：STP＝停損、MIT＝停利

進場成交後（`entry_price > 0`），依序嘗試掛兩張真正掛在券商端的智慧單：

| 智慧單 | 用途 | 觸發條件 | 對應函式 |
|---|---|---|---|
| **STP** | 停損 | 多單：進場價 − 100 點；空單：進場價 + 100 點 | `_place_stop_order_for_state` |
| **MIT** | 停利 | 多單：進場價 + 300 點；空單：進場價 − 300 點 | `_place_mit_order_for_state` |

（`stop_loss_points=100.0`、`take_profit_points=300.0` 是 `StrategyState` 的
預設值，`backend/strategy_service.py` 第 80-81 行）

- 兩張都是**限價智慧單**（`order_price_type: 2`），觸發後以指定價格試圖成交。
- 智慧單掛在**券商端**，跟我們的後端程式是否還在跑無關——就算 `tmf-backend.exe`
  當掉、電腦重開機，這張單還是活的，這是它跟「軟停損」最大的差別。
- **限制**：STP/MIT 目前只驗證過商品代碼是具體月份格式的 TMF（例如
  `TM2609`），連續代碼（`TX00`/`MXFR1` 這種）會直接跳過智慧單、改用軟停損停利
  頂著（`_settlement_month_from_tmf_code` 回傳 `None` 時，見
  `backend/strategy_service.py` 第 202-213 行）。
- 掛單失敗（送單被拒、回應格式解析不出來、或發生例外）一律**靜默退回軟停損
  停利**，不會讓整個進場流程失敗——`state.last_action` 會附註原因，方便事後
  核對。

## 4. 軟停損停利（智慧單掛不成時的備援層）

`_tick_one` 每一輪（strategy_loop 目前是每 **60 秒**跑一次）都會檢查：

```
points = 現價 - 進場價（多單）或 進場價 - 現價（空單）

if points <= -100 且沒有真實 STP 頂著:
    觸發停損 → 送市價 FOK 平倉單
elif points >= 300 且沒有真實 MIT 頂著:
    觸發停利 → 送市價 FOK 平倉單
elif 浮動損益觸及風控門檻（見第 5 節）:
    觸發風控保險 → 送市價 FOK 平倉單
```
（`backend/strategy_service.py` 第 776-834 行）

- 有真實 STP/MIT 頂著時，軟停損停利會**讓路**（不重複判斷同一種觸發），避免
  兩邊搶著平倉。
- 這個檢查**不管策略是否已經因為其他原因被停止都會跑**——只要 `held_qty>0`
  就會繼續保護，這是刻意設計（見 `stop_strategy()` 的說明：手動關開關如果還
  有留倉，不會把策略整個移出監控清單）。
- 一旦觸發，直接送真實市價平倉單，不等反向訊號、不等下一輪。

## 5. 額外一層風控保險：NTD / 百分比虧損上限

點數停損是主要判斷依據，但正常情況下點數停損會先觸發；如果跳空、或多口交易
時單口損失金額換算後比較寬鬆，點數還沒到，浮動虧損金額/比例已經超過帳戶能
承受範圍——這時會強制停止，不等點數：

- `STRATEGY_MAX_LOSS_NTD`（預設 10,000 元）
- `STRATEGY_MAX_LOSS_PCT`（預設 10%，相對 `STRATEGY_INITIAL_CAPITAL`）

兩個門檻任一觸及就強制平倉（`StrategyState.loss_limit_hit()`，
`backend/strategy_service.py` 第 106-114 行）。

## 6. 「委託回報失敗，但偵測到近期有相符成交」是什麼

這是**第三層安全網**，處理「我方軟體回報下單失敗，但券商端其實已經受理/成交」
這種狀況（`_check_orphaned_fill`，見 2026-07-27 的原始事故說明）。命中時會：

1. 大聲停用該策略，`last_action` 顯示警告，要求人工核對
2. **不會**設定 `held_qty`/`held_direction`（無法安全判斷這筆單該算進哪個策略，
   多個策略可能同時交易同一商品）
3. 因此也**不會**掛 STP/MIT、**不會**有軟停損停利保護——這筆倉位如果真的存在，
   在人工核對/平倉之前完全沒有自動保護（見 2026-08-25 的真實案例：51 分鐘
   完全無保護，靠人工發現警告後手動平倉才化解）

`send_future_order`/`cancel_order_by_seqno`/`correct_price_by_seqno` 的
message/code 順序 bug 已於 2026-08-25 修復（詳見程式碼內對應日期註解），這個
安全網原本最常見的觸發源已經消除，但「軟體真的無法判斷結果」的情境（例如
網路逾時）理論上還是有可能命中，這一層的殘留風險目前尚未進一步處理。
