# 四道出場防護網獨立開關設計

日期：2026-08-31
狀態：待使用者核准

## 背景

系統對每一筆留倉理論上疊了四道出場防護：

- **Layer 1** 券商 OCO 智慧單（`_place_oco_protection_for_state`，進場成交後掛，成功的話停損停利兩支腿真的掛在券商端）
- **Layer 2** 軟停損停利備援（`_tick_one` 逐輪比價，只在 Layer 1 沒生效時頂上）
- **Layer 3** 金額/比例硬停損保險（`state.loss_limit_hit()`，浮動損益超過 NTD 或 % 上限就強制平倉，不管點數有沒有到）
- **Layer 4** 反向訊號出場（方向限定策略看到反向訊號時把留倉平掉）

今天稍早在同一個方向上試了兩次都出問題：第一次用單一字串欄位 `exit_mode` 塞多種模式，`"sltp_only"`（我加的，只想關 Layer 4）跟後來加的 `"sltp_fixed"`/`"signal_only"`（Gemini 加的，用來關 Layer 2/3）撞名，造成某些帳號在不知情下被同時關掉 Layer 2/3；第二次乾脆把整個 `exit_mode` 拿掉，回到「四層永遠全開、不能關」。

使用者現在明確要求：四層做成 4 個各自獨立的布林開關，可依帳號/策略客製化，預設全部 `True`（開啟，行為跟現在完全一樣）；且確認四層完全對等，包含 Layer 3 也可以關——不設任何強制項。

## 目標

- 4 個獨立布林欄位，語意互不重疊，不會再撞名。
- 每個帳號＋策略的組合可以各自客製化開關，透過現有的 `user_strategy_configs` 綁定機制。
- 預設值全部 `True`，任何既有帳號、既有呼叫路徑不傳新參數時行為與今天完全一致。
- 四層完全對等，沒有任何一層是強制、不可關閉的。

## 非目標（明確排除，避免範圍蔓延）

- `_tick_one` 裡「空手時風控」那段（`state.held_qty == 0 and state.loss_limit_hit()`，第 1005 行；以及訊號處理完之後的收尾檢查，第 1199 行）檢查的是**累計已實現損益**，觸發時是停止策略、不是平掉某一筆留倉——這是「虧多了就不准再開新倉」的機制，不是「出場防護網」，跟這次的 4 層無關，維持現狀不受任何開關影響。
- 不重新設計四層的觸發邏輯或優先順序，只加開關。
- 不處理「反向訊號出場該不該把同方向所有策略的倉位都平掉」那個獨立問題（之前腦力激盪中斷、尚未有結論），本次維持現狀：只平自己策略的 `held_qty`。

## 資料模型

`StrategyState`（`backend/strategy_service.py`）新增 4 個欄位，緊接在既有欄位群組旁：

```python
oco_enabled: bool = True
soft_stop_enabled: bool = True
risk_insurance_enabled: bool = True
reverse_signal_exit_enabled: bool = True
```

每個欄位上方各加一行簡短註解說明對應哪一層、關掉會怎樣——比照這個檔案現有的註解密度，不需要長篇。

## 觸發點改法

**Layer 1（OCO）**——`_tick_one` 第 1130 行呼叫處，加一個條件：

```python
if state.entry_price > 0 and state.oco_enabled:
    _place_oco_protection_for_state(state, svc)
```

**Layer 2 / Layer 3（第 913-921 行的 if/elif 鏈）**——現狀本來就是三個獨立條件用 `elif`串優先順序（先比停損、再比停利、最後比風控保險），改法是直接在各自的條件式前面加開關判斷，結構不用大動：

```python
if state.soft_stop_enabled and points <= -abs(state.stop_loss_points) and state.protection_order_smart_key is None:
    trigger_kind = "stop_loss"
elif state.soft_stop_enabled and points >= abs(state.take_profit_points) and state.protection_order_smart_key is None:
    trigger_kind = "take_profit"
elif state.risk_insurance_enabled and floating_pnl is not None and state.loss_limit_hit(floating_pnl):
    trigger_kind = "risk_stop"
```

`soft_stop_enabled=False` 時兩個點數條件都不會成立，`risk_insurance_enabled=False` 時風控保險條件不會成立——兩個開關各自只影響自己控制的條件，`elif` 鏈的優先順序不受影響，不需要拆成三段獨立 if。

**Layer 4（反向訊號出場）**——第 1134-1198 行「方向限定策略：出現反向訊號只平倉出場」整段，外層包一個開關：

```python
elif not state.reverse_signal_exit_enabled:
    state.last_action = "偵測到反向訊號，但本策略已關閉反向訊號出場，不動作"
else:
    if state.held_qty > 0:
        ...（原本的平倉邏輯不變）
    else:
        state.last_action = "空手，反向訊號不動作（本策略不做這個方向）"
```

## 資料流與客製化管線

沿用今天稍早已經打通的 `user_strategy_configs` 綁定管線（`stop_loss_points`/`take_profit_points`/`max_loss_ntd`/`max_loss_pct` 那一輪的模式），4 個新開關比照辦理：

1. **SQL migration**（新檔，additive）：`supabase/sql/2026-08-31_add_layer_toggles_to_user_strategy_configs.sql`

   ```sql
   alter table if exists public.user_strategy_configs
     add column if not exists oco_enabled boolean not null default true,
     add column if not exists soft_stop_enabled boolean not null default true,
     add column if not exists risk_insurance_enabled boolean not null default true,
     add column if not exists reverse_signal_exit_enabled boolean not null default true;
   ```

   跟數值型參數不同，這裡用 `not null default true`（而非允許 NULL）——布林開關沒有「未設定」的自然意義，直接讓資料庫層面保證預設就是全開，比在應用層層層判斷 None 更不容易漏掉。

2. **`start_strategy()`**（`backend/strategy_service.py`）新增 4 個關鍵字參數，型別 `bool | None = None`，跟現有數值參數一樣走「None 用 `strategy_config_defaults()` 的值」的模式；`strategy_config_defaults()` 回傳的預設值固定為 `True`（不像數值參數會讀 `.env`，這裡不需要環境變數層級的覆寫）。

3. **`admin_upsert_strategy_config()`**（`backend/strategy_config_store.py`）新增 4 個 `bool | None = None` 參數，比照現有數值參數「不是 None 才放進 payload」的寫法。

4. **`_auto_arm_bound_strategies()`**（`backend/api.py`）呼叫 `start_strategy` 時多傳 4 個 `row.get(...)`。

5. **`scripts/bind_strategy.py`** CLI 新增 4 個 flag，用 `argparse.BooleanOptionalAction`（例如 `--oco-enabled` / `--no-oco-enabled`），預設 `None`（不覆寫，沿用資料庫預設的 `True`），跟現有 `--stop-loss-points` 等參數的「不指定就不覆寫」語意一致。

6. **`_state_to_dict()`**：新增 4 個 key，讓 Dashboard 前端未來要顯示/切換這幾個開關時，資料已經在 `/api/strategy/status` 回傳裡。這次不做前端 UI（使用者只要求後端邏輯與 CLI／DB 綁定，前端開關按鈕留待之後有需要再做）。

## 邊界情況與風險

- 四層全部關閉是合法狀態（使用者已確認接受）：那筆倉位完全沒有任何自動出場保護，只能靠人工操作。程式不擋這個組合，但 `docs/live_trading_flow.md` 需要補一段說明這個風險（寫 spec 時一併排入實作計畫）。
- `oco_enabled=False` 時完全不呼叫 `_place_oco_protection_for_state`，代表 `protection_order_smart_key` 永遠是 `None`——Layer 2 的「`protection_order_smart_key is None` 才頂上」判斷不受影響，邏輯上會直接讓軟停損頂替，行為符合預期（等於「這筆倉位只靠軟停損保護，不靠券商 OCO」）。
- 既有的孤兒單清理（`reconcile_orphan_stop_orders`）不受影響：它只清「券商端還掛著、但內部認為不該存在」的單，跟這 4 個開關無關（開關只決定要不要「掛新單／跑檢查」，不影響已經掛出去的單要不要撤）。

## 測試

每一層各兩組（開啟時正常運作 / 關閉時整層讓路），共 8 組，比照 `tests/test_strategy_service.py` 現有的 `_tick_one` 測試模式（用 `MagicMock` 的 `TradingService`，直接組 `StrategyState` 檢查副作用）：

- `test_tick_one_places_oco_when_oco_enabled` / `test_tick_one_skips_oco_when_oco_disabled`
- `test_tick_one_soft_stop_triggers_when_enabled` / `test_tick_one_soft_stop_skipped_when_disabled`
- `test_tick_one_risk_insurance_triggers_when_enabled` / `test_tick_one_risk_insurance_skipped_when_disabled`
- `test_tick_one_reverse_signal_exit_closes_when_enabled` / `test_tick_one_reverse_signal_exit_skipped_when_disabled`

加上管線傳遞測試：`start_strategy` 傳入非預設值時 `StrategyState` 欄位確實被覆寫；`admin_upsert_strategy_config` payload 組裝測試（比照現有 `stop_loss_points` 那組測試）。

實作完成後，跑一次全套 `pytest tests/ -q` 確認沒有回歸（尤其是既有的 4 層相關舊測試，例如 `test_tick_one_reverse_signal_closes_position_by_default`，預設值不變的情況下應該原封不動通過）。
