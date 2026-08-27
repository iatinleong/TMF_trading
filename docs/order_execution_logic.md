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

## 3. 智慧單：單一 OCO 委託同時掛停損＋停利

**2026-08-27 變更**：原本進場後分別呼叫兩張獨立的智慧單（STP 停損＋MIT
停利），改成呼叫 `SendFutureOCOOrderV1` **一次委託同時掛兩支腳**。

### 為什麼要改

STP 跟 MIT 各自都是「平倉」性質（`new_close=1`）的智慧單。兩張各自獨立的
平倉智慧單同時掛在同一口（`qty=1`）持倉上，第一張成功掛上後就把這口部位
的可平倉數量佔滿，第二張送單會被券商拒絕：

```
[999] 勾選平倉而留倉部位不足
```

這不是我們程式碼的 bug，是券商對「同一部位掛多張平倉智慧單」的限制——
2026-08-27 的真實事故（見第 7 節）就是因為 STP 先掛成功、MIT 送單被拒，
且事後對帳邏輯又誤判把已經掛好的 STP 撤掉，導致部位裸奔 30+ 分鐘。

2026-08-27 用「絕不會被觸發成交」的方式在正式環境實測驗證：`SendFutureOCOOrderV1`
把兩支腳包在**同一個委託**裡送出，券商內部會把它們當成一組連動單處理
（一腳觸發，另一腳自動取消），不會有「搶佔平倉額度」的問題。

### 怎麼掛

| 項目 | 內容 |
|---|---|
| 對應函式 | `_place_oco_protection_for_state`（`backend/strategy_service.py`） |
| 券商 API | `CapitalFuturesBroker.send_future_oco_order()` → `SendFutureOCOOrderV1` |
| 停損觸發 | 多單：進場價 − 100 點；空單：進場價 + 100 點 |
| 停利觸發 | 多單：進場價 + 250 點；空單：進場價 − 250 點 |

（`stop_loss_points=100.0`、`take_profit_points=250.0` 是 `StrategyState` 的
預設值，`backend/strategy_service.py` 第 80-81 行；2026-08-27 跟使用者核對過
正式規格改為 -100/+250，原本寫死是 -100/+300）

- **兩支腳都是限價**（`order_price_type: 2`），觸發後以指定價格試圖成交。
  範圍市價（`order_price_type: 3` + `price="P"`）在 ROD 條件下實測會被拒
  （`[519] 委託條件為ROD時,請輸入委託價(差)`），所以沒有採用。
- **兩支腳的順序是依「觸發價數值大小」決定，不是依語意（停損/停利）決定**：
  券商實測規則是「第一腳觸發價必須比第二腳高」（`[999] OCO第二隻腳觸發價
  不能大於等於第一隻腳`），所以多單/空單的第一腳分別是停利/停損，順序會
  互換——不要假設「第一腳固定是停損」。
- 一次委託成功後只追蹤**一組**券商端單號（`protection_order_smart_key` /
  `_seq_no` / `_order_no`），撤單也只需呼叫一次
  `_cancel_protection_order_for_state`（`trade_kind=3`／OCO）。
- 智慧單掛在**券商端**，跟我們的後端程式是否還在跑無關——就算 `tmf-backend.exe`
  當掉、電腦重開機，這張單還是活的，這是它跟「軟停損」最大的差別。
- **限制**：OCO 目前只驗證過商品代碼是具體月份格式的 TMF（例如
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

if points <= -100 且沒有真實 OCO 保護單頂著:
    觸發停損 → 送市價 FOK 平倉單
elif points >= 250 且沒有真實 OCO 保護單頂著:
    觸發停利 → 送市價 FOK 平倉單
elif 浮動損益觸及風控門檻（見第 5 節）:
    觸發風控保險 → 送市價 FOK 平倉單
```
（`backend/strategy_service.py` 第 776-834 行）

- 有真實 OCO 保護單（`protection_order_smart_key` 不是 `None`）頂著時，軟
  停損停利會**讓路**（不重複判斷同一種觸發），避免兩邊搶著平倉。
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

## 7. 2026-08-27 真實事故：對帳誤判把剛掛好的真實 STP 撤掉，留下裸倉 30+ 分鐘

**症狀**：`pullback_long` 訊號成立（已核對過 K 線/均線資料，訊號本身沒有問題）
→ 市價進場成交（46158）→ STP 停損智慧單成功掛上（15:00:06）→ MIT 停利智慧單
被券商拒絕，原因「勾選平倉而留倉部位不足」（15:00:09，見下方待查事項）→
**8 秒後（15:00:14），已經成功掛上的 STP 被系統自己撤掉**，策略停止，訊息顯示
「偵測到 TM2609 long 部位跟券商實際回報不一致」→ 這筆倉位之後 30 分鐘以上完全
沒有任何保護（沒有真實 STP/MIT，軟停損停利也不會跑，因為策略已經被
`_stop_with_reason` 停用）。

**根因**：`reconcile_after_manual_close()`（第 687 行，`strategy_tick()`
每輪都會呼叫）拿 `state.product_code`（策略內部記帳用，TMF 具體月份碼格式
`"TM2609"`）直接跟群益 `OnOpenInterest`／`GetOpenInterestGW` 回報的
`position["product"]` 字串比對——但這個查詢事件對同一張 TMF 月份倉位回傳的是
**不含年份的短碼 `"TM09"`**（`backend/capital_parse.py::parse_open_interest_line`
第 93 行單純原樣沿用券商回傳的字串，沒有正規化）。兩個字串必然對不上，於是
一張完全對得上的真實持倉，被誤判成「內部記錄 1 口，券商實際回報 0 口」，觸發
「寧可停下來讓人核對」的保守處理：清空 `held_qty`、撤掉 STP/MIT、停用策略。

查過群益官方文件（`7.下單-國內期選.docx`）沒有找到明確説明「TMF 月份碼在這個
事件裡會被省略年份」這件事——這個行為是從實際回傳資料反推出來的，不是文件
上寫的，只能當作經驗證過的既有事實，原因不明。

**這不是單一事故，是系統性 bug**：任何用具體月份碼（`TM2609` 這種格式）交易
TMF 的策略，每次成功進場後的下一輪對帳都會誤判觸發。既有測試沒抓到是因為
測試固定用 `"TMFR1"`（連續代碼，兩邊字串剛好一樣，不會觸發這個問題）——見
`tests/test_strategy_service.py::test_reconcile_after_manual_close_matches_broker_short_month_product_code`
用真實事故的兩種格式重現過一次。

**修法**：新增 `_canonical_position_product()`，兩邊比對前都先轉成券商回報的
短碼格式（`"TM2609"` → `"TM09"`）再比較（`backend/strategy_service.py`）。

**How to apply**：以後任何地方要拿 `state.product_code` 去跟券商回報的
`position`/`positions_raw` 資料比對，一律先過 `_canonical_position_product()`，
不要直接比字串——這個教訓就是因為兩套 API（下單 vs. 查詢未平倉）對同一個
商品用不同字串格式，直接比對必定出錯。

**還沒查清楚、待處理**：MIT 被拒絕的原因「勾選平倉而留倉部位不足」——目前
懷疑是群益不允許對同一口（qty=1）持倉，同時掛兩張各自獨立的平倉類智慧單
（STP 先佔用了這 1 口的可平倉額度，MIT 再想佔用同一口就沒有額度了），但這個
推論沒有找到官方文件證實，也還沒問過群益。在確認並修好之前，**qty=1 的
TMF 倉位，MIT 停利智慧單大概率會掛不上，只能靠軟停利頂著**（見第 4 節）。
