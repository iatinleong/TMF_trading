# 四道出場防護網獨立開關 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把「四道出場防護網」（Layer 1 券商 OCO、Layer 2 軟停損停利備援、Layer 3 金額/比例硬停損保險、Layer 4 反向訊號出場）各自做成獨立布林開關，預設全開，可依帳號/策略客製化關閉。

**Architecture:** `StrategyState` 新增 4 個獨立布林欄位（不用字串列舉，避免撞名），`_tick_one()` 裡四個既有觸發點各自加上對應開關判斷；透過 `user_strategy_configs` 表 + `start_strategy()` + `admin_upsert_strategy_config()` + `bind_strategy.py` CLI + `/api/strategy/start` 這條既有的客製化參數管線，把開關值從資料庫一路傳到執行時的 `StrategyState`。

**Tech Stack:** Python 3.13、FastAPI、pandas、pytest、Supabase（PostgREST）。

## Global Constraints

- 4 個欄位全部命名為 `oco_enabled` / `soft_stop_enabled` / `risk_insurance_enabled` / `reverse_signal_exit_enabled`，全部 `bool`，預設 `True`。
- 不使用字串列舉（不重蹈 `exit_mode` 撞名覆轍）。
- 資料庫欄位用 `not null default true`（不是 nullable，見 spec）。
- 空手時累計損益風控（`_tick_one` 第 1005、1199 行附近）不受這 4 個開關影響，維持現狀——不在本計畫範圍內。
- 每個檔案的既有中文註解風格、密度要延續，不要用英文取代。
- 每個 Task 完成後才能進到下一個；每個 Task 結尾都要 commit。

---

### Task 1: `StrategyState` 新增 4 個獨立開關欄位

**Files:**
- Modify: `backend/strategy_service.py:80-134`（`StrategyState` dataclass）、`backend/strategy_service.py:597-609`（`strategy_config_defaults()`）、`backend/strategy_service.py:612-641`（`_state_to_dict()`）
- Test: `tests/test_strategy_service.py`

**Interfaces:**
- Produces: `StrategyState.oco_enabled: bool`、`.soft_stop_enabled: bool`、`.risk_insurance_enabled: bool`、`.reverse_signal_exit_enabled: bool`，全部預設 `True`；`strategy_config_defaults()` 回傳 dict 多 4 個 key，值固定 `True`；`_state_to_dict()` 回傳 dict 多 4 個同名 key。

- [ ] **Step 1: 寫失敗測試**

在 `tests/test_strategy_service.py` 找一個現有的、測試 `StrategyState` 預設值或 `strategy_config_defaults()` 的測試附近（例如 `test_strategy_loss_limit` 之前），加入：

```python
def test_strategy_state_layer_toggles_default_to_true():
    state = _open_long_state()
    assert state.oco_enabled is True
    assert state.soft_stop_enabled is True
    assert state.risk_insurance_enabled is True
    assert state.reverse_signal_exit_enabled is True


def test_strategy_config_defaults_includes_layer_toggles():
    defaults = strategy_config_defaults("breakout_long")
    assert defaults["oco_enabled"] is True
    assert defaults["soft_stop_enabled"] is True
    assert defaults["risk_insurance_enabled"] is True
    assert defaults["reverse_signal_exit_enabled"] is True


def test_state_to_dict_includes_layer_toggles():
    state = _open_long_state(oco_enabled=False, reverse_signal_exit_enabled=False)
    result = strategy_service._state_to_dict(state)
    assert result["oco_enabled"] is False
    assert result["soft_stop_enabled"] is True
    assert result["risk_insurance_enabled"] is True
    assert result["reverse_signal_exit_enabled"] is False
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `python -m pytest tests/test_strategy_service.py -k layer_toggles -v`
Expected: FAIL，`TypeError: __init__() got an unexpected keyword argument 'oco_enabled'`（或 `AttributeError`/`KeyError`，因為欄位還不存在）。

- [ ] **Step 3: 加欄位**

在 `backend/strategy_service.py` 的 `StrategyState` dataclass 裡，`consecutive_failures: int = 0` 那行之後加：

```python
    # 2026-08-31：四道出場防護網（Layer 1 OCO／Layer 2 軟停損停利／Layer 3
    # 金額比例硬停損／Layer 4 反向訊號出場）各自獨立的開關，可依帳號/策略
    # 客製化關閉。刻意用 4 個獨立布林欄位而不是單一字串列舉——今天稍早用
    # exit_mode 字串列舉時，兩組不同用途的字串值互相撞名，導致某些帳號被
    # 靜默關掉不該關的防護層（見 docs/superpowers/specs/2026-08-31-
    # per-layer-protection-toggles-design.md）。四層完全對等，預設全部
    # True（開啟），任何既有呼叫不傳這 4 個參數時行為與過去完全一致。
    oco_enabled: bool = True
    soft_stop_enabled: bool = True
    risk_insurance_enabled: bool = True
    reverse_signal_exit_enabled: bool = True
```

在 `strategy_config_defaults()` 回傳的 dict 裡，`"take_profit_points": ...` 那行之後加：

```python
        "oco_enabled": True,
        "soft_stop_enabled": True,
        "risk_insurance_enabled": True,
        "reverse_signal_exit_enabled": True,
```

在 `_state_to_dict()` 回傳的 dict 裡，`"take_profit_points": state.take_profit_points,` 那行之後加：

```python
        "oco_enabled": state.oco_enabled,
        "soft_stop_enabled": state.soft_stop_enabled,
        "risk_insurance_enabled": state.risk_insurance_enabled,
        "reverse_signal_exit_enabled": state.reverse_signal_exit_enabled,
```

- [ ] **Step 4: 執行測試確認通過**

Run: `python -m pytest tests/test_strategy_service.py -k layer_toggles -v`
Expected: PASS（3 個測試）

- [ ] **Step 5: Commit**

```bash
git add backend/strategy_service.py tests/test_strategy_service.py
git commit -m "feat: add 4 independent per-layer protection toggle fields to StrategyState

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_018YRXPuiMdg4mouz4pBsXWb"
```

---

### Task 2: Layer 1（券商 OCO）開關

**Files:**
- Modify: `backend/strategy_service.py:1130-1131`
- Test: `tests/test_strategy_service.py`

**Interfaces:**
- Consumes: Task 1 的 `StrategyState.oco_enabled`。

- [ ] **Step 1: 寫失敗測試**

在 `test_tick_one_entry_places_oco_protection` 附近加：

```python
def test_tick_one_entry_skips_oco_when_disabled(clean_armed):
    state = _open_long_state(
        product_code="TM2609", held_qty=0, held_direction=None, entry_price=0.0,
        oco_enabled=False,
    )
    state.last_signal_key = None
    svc = MagicMock()
    svc.place_order.return_value = {"order_result": {"success": True}}
    st = {"quote": {"last_price": 20000.0}}

    fake_bars = MagicMock()
    fake_bars.empty = False
    fake_bars.__len__.return_value = 100

    with patch.object(
        strategy_service,
        "_latest_closed_signal",
        return_value={"time": "2026-08-21T09:45:00", "direction": "long", "key": "k1"},
    ), patch.object(strategy_service, "_load_bars", return_value=fake_bars), patch.object(
        strategy_service, "_signaled_frame", return_value=MagicMock()
    ):
        _tick_one(state, svc, st)

    assert state.held_qty == 1
    svc.place_oco_order.assert_not_called()
    assert state.protection_order_smart_key is None
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `python -m pytest tests/test_strategy_service.py -k skips_oco_when_disabled -v`
Expected: FAIL，`svc.place_oco_order.assert_not_called()` 失敗（因為現在還是無條件呼叫）。

- [ ] **Step 3: 加開關**

`backend/strategy_service.py` 第 1130-1131 行，原本：

```python
                    if state.entry_price > 0:
                        _place_oco_protection_for_state(state, svc)
```

改成：

```python
                    if state.entry_price > 0 and state.oco_enabled:
                        _place_oco_protection_for_state(state, svc)
```

- [ ] **Step 4: 執行測試確認通過**

Run: `python -m pytest tests/test_strategy_service.py -k "skips_oco_when_disabled or entry_places_oco" -v`
Expected: PASS（新測試 + 既有的 `test_tick_one_entry_places_oco_protection` 都過，確認預設 `True` 時行為不變）。

- [ ] **Step 5: Commit**

```bash
git add backend/strategy_service.py tests/test_strategy_service.py
git commit -m "feat: gate Layer 1 broker OCO placement behind oco_enabled toggle

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_018YRXPuiMdg4mouz4pBsXWb"
```

---

### Task 3: Layer 2（軟停損停利）與 Layer 3（風控保險）開關

**Files:**
- Modify: `backend/strategy_service.py:913-921`
- Test: `tests/test_strategy_service.py`

**Interfaces:**
- Consumes: Task 1 的 `StrategyState.soft_stop_enabled`、`.risk_insurance_enabled`。

- [ ] **Step 1: 寫失敗測試**

在 `test_tick_one_risk_stop_fallback_triggers_before_point_stop` 附近加兩個測試：

```python
def test_tick_one_soft_stop_skipped_when_disabled():
    # 跟 test_tick_one_forces_close_on_stop_loss_points 同一組價位（跌 200
    # 點，遠超預設 stop_loss_points=100），但 soft_stop_enabled=False 時
    # 不該觸發平倉。max_loss_pct 拉高避免順便撞到風控保險。
    state = _open_long_state(soft_stop_enabled=False, max_loss_ntd=100_000.0)
    svc = MagicMock()
    st = {"quote": {"last_price": 19800.0}}

    _tick_one(state, svc, st)

    svc.place_order.assert_not_called()
    assert state.held_qty == 1
    assert state.stopped is False


def test_tick_one_risk_insurance_skipped_when_disabled():
    # 跟 test_tick_one_risk_stop_fallback_triggers_before_point_stop 同一組
    # 場景（點數停損門檻拉很寬，浮動虧損金額超過 max_loss_ntd），但
    # risk_insurance_enabled=False 時不該觸發平倉。
    state = _open_long_state(
        stop_loss_points=1000.0, take_profit_points=3000.0,
        max_loss_ntd=500.0, max_loss_pct=0.99,
        risk_insurance_enabled=False,
    )
    svc = MagicMock()
    st = {"quote": {"last_price": 19950.0}}

    _tick_one(state, svc, st)

    svc.place_order.assert_not_called()
    assert state.held_qty == 1
    assert state.stopped is False
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `python -m pytest tests/test_strategy_service.py -k "soft_stop_skipped_when_disabled or risk_insurance_skipped_when_disabled" -v`
Expected: FAIL（兩個都因為現在無條件觸發平倉，`svc.place_order.assert_not_called()` 失敗）。

- [ ] **Step 3: 加開關**

`backend/strategy_service.py` 第 913-921 行，原本：

```python
            if points <= -abs(state.stop_loss_points) and state.protection_order_smart_key is None:
                # Layer 2 (軟停損備援)：券商端 OCO 未生效時頂著
                trigger_kind = "stop_loss"
            elif points >= abs(state.take_profit_points) and state.protection_order_smart_key is None:
                # Layer 2 (軟停利備援)
                trigger_kind = "take_profit"
            elif floating_pnl is not None and state.loss_limit_hit(floating_pnl):
                # Layer 3 (風控保險網 / 硬停損)
                trigger_kind = "risk_stop"
```

改成：

```python
            if state.soft_stop_enabled and points <= -abs(state.stop_loss_points) and state.protection_order_smart_key is None:
                # Layer 2 (軟停損備援)：券商端 OCO 未生效時頂著
                trigger_kind = "stop_loss"
            elif state.soft_stop_enabled and points >= abs(state.take_profit_points) and state.protection_order_smart_key is None:
                # Layer 2 (軟停利備援)
                trigger_kind = "take_profit"
            elif state.risk_insurance_enabled and floating_pnl is not None and state.loss_limit_hit(floating_pnl):
                # Layer 3 (風控保險網 / 硬停損)
                trigger_kind = "risk_stop"
```

- [ ] **Step 4: 執行測試確認通過**

Run: `python -m pytest tests/test_strategy_service.py -k "soft_stop or risk_insurance or risk_stop_fallback or forces_close" -v`
Expected: PASS——包含新的 2 個 disabled 測試，以及既有的 `test_tick_one_forces_close_on_stop_loss_points`、`test_tick_one_forces_close_on_take_profit_points`、`test_tick_one_risk_stop_fallback_triggers_before_point_stop`（確認預設 `True` 時這三個既有測試不受影響、原封不動通過）。

- [ ] **Step 5: Commit**

```bash
git add backend/strategy_service.py tests/test_strategy_service.py
git commit -m "feat: gate Layer 2/3 soft-stop and risk-insurance checks behind independent toggles

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_018YRXPuiMdg4mouz4pBsXWb"
```

---

### Task 4: Layer 4（反向訊號出場）開關

**Files:**
- Modify: `backend/strategy_service.py:1134-1198`
- Test: `tests/test_strategy_service.py:800-825`（含既有測試 docstring 修正）

**Interfaces:**
- Consumes: Task 1 的 `StrategyState.reverse_signal_exit_enabled`。

- [ ] **Step 1: 修正既有測試的過期 docstring，並寫新的失敗測試**

`tests/test_strategy_service.py` 現有的 `test_tick_one_reverse_signal_closes_position_by_default`（第 800-804 行）docstring 還提到已經在 2026-08-31 被完全移除的 `exit_mode` 欄位，先修正：

原本：

```python
def test_tick_one_reverse_signal_closes_position_by_default(clean_armed):
    """
    2026-08-31：預設（`exit_mode` 未設定，或設成 "signal_only"）維持原本行為——
    方向限定策略遇到反向訊號，有留倉就平倉出場。
    """
```

改成：

```python
def test_tick_one_reverse_signal_closes_position_by_default(clean_armed):
    """
    reverse_signal_exit_enabled 預設 True，方向限定策略遇到反向訊號、有留倉
    時應該平倉出場（見 test_tick_one_reverse_signal_skipped_when_disabled
    驗證關掉之後的行為）。
    """
```

在它後面加新測試：

```python
def test_tick_one_reverse_signal_skipped_when_disabled(clean_armed):
    state = _open_long_state(product_code="TM2609", reverse_signal_exit_enabled=False)
    svc = MagicMock()
    st = {"quote": {"last_price": 20050.0}}

    fake_bars = MagicMock()
    fake_bars.empty = False
    fake_bars.__len__.return_value = 100

    with patch.object(
        strategy_service,
        "_latest_closed_signal",
        return_value={"time": "2026-08-21T09:45:00", "direction": "short", "key": "k1"},
    ), patch.object(strategy_service, "_load_bars", return_value=fake_bars), patch.object(
        strategy_service, "_signaled_frame", return_value=MagicMock()
    ):
        _tick_one(state, svc, st)

    svc.place_order.assert_not_called()
    assert state.held_qty == 1
    assert "已關閉反向訊號出場" in state.last_action


def test_tick_one_reverse_signal_flat_position_unaffected_by_toggle(clean_armed):
    # 空手時遇到反向訊號，不管開關是 True 還是 False，訊息都該是「空手不
    # 動作」，不能被誤導成「已關閉反向出場」（review 抓到的巢狀結構要求）。
    state = _open_long_state(
        product_code="TM2609", held_qty=0, held_direction=None, entry_price=0.0,
        reverse_signal_exit_enabled=False,
    )
    state.last_signal_key = None
    svc = MagicMock()
    st = {"quote": {"last_price": 20050.0}}

    fake_bars = MagicMock()
    fake_bars.empty = False
    fake_bars.__len__.return_value = 100

    with patch.object(
        strategy_service,
        "_latest_closed_signal",
        return_value={"time": "2026-08-21T09:45:00", "direction": "short", "key": "k1"},
    ), patch.object(strategy_service, "_load_bars", return_value=fake_bars), patch.object(
        strategy_service, "_signaled_frame", return_value=MagicMock()
    ):
        _tick_one(state, svc, st)

    svc.place_order.assert_not_called()
    assert state.last_action == "空手，反向訊號不動作（本策略不做這個方向）"
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `python -m pytest tests/test_strategy_service.py -k "reverse_signal_skipped_when_disabled or reverse_signal_flat_position" -v`
Expected: FAIL（`test_tick_one_reverse_signal_skipped_when_disabled` 因為現在無條件平倉；`test_tick_one_reverse_signal_flat_position_unaffected_by_toggle` 目前應該剛好通過，因為現有程式碼本來就沒有這個開關判斷——這個測試先寫著卡位，等 Step 3 改完程式碼後仍要保持通過，用來防止之後有人把開關判斷誤放到外層）。

- [ ] **Step 3: 加開關（開關放在 `held_qty > 0` 分支內側，不是最外層）**

`backend/strategy_service.py` 第 1134-1198 行，原本（完整內容）：

```python
        else:
            # 方向限定策略：出現反向訊號只平倉出場，不反手做另一邊
            if state.held_qty > 0:
                close_side = "sell" if state.held_direction == "long" else "buy"
                result = svc.place_order(
                    {
                        "product_code": state.product_code,
                        "side": close_side,
                        "qty": state.held_qty,
                        "price": "M",
                        "trade_type": 2,
                        "new_close": 1,
                    }
                )
                order_result = result.get("order_result") or {}
                if not order_result.get("success"):
                    state.last_signal_key = signal["key"]
                    state.consecutive_failures += 1
                    state.last_action = f"平倉失敗（{state.held_direction}）: {order_result.get('message') or '券商拒單'}"
                    if _check_orphaned_fill(
                        result.get("state") or {},
                        expected_side=("short" if close_side == "sell" else "long"),
                        qty=state.held_qty,
                    ):
                        # 2026-08-21 修正：這裡跟上面持倉停損停利那段的孤兒單處理不一致，
                        # 之前只有停止、沒有清空 held_qty——代表偵測到「其實已經平倉」
                        # 之後，下一輪還是會當作有留倉繼續處理，等於白偵測。統一改成
                        # 跟上面一樣清空。
                        state.held_qty = 0
                        state.held_direction = None
                        state.entry_price = 0.0
                        _cancel_protection_order_for_state(state, svc)
                        _stop_with_reason(
                            state,
                            "⚠ 平倉委託回報失敗，但偵測到近期有相符的真實成交紀錄，"
                            "這筆倉位可能其實已經平倉、或狀態跟我們追蹤的不一致，"
                            "已自動停止本策略，請立即人工核對！",
                        )
                else:
                    pnl = None
                    if state.entry_price > 0 and quote:
                        pnl = _estimate_pnl_ntd(
                            contract=contract,
                            direction=str(state.held_direction),
                            entry_price=state.entry_price,
                            exit_price=float(quote),
                            qty=state.held_qty,
                        )
                        state.realized_pnl_ntd += pnl
                    _record_trade(
                        state.strategy_id,
                        kind="exit",
                        bar_time=signal["time"],
                        direction=str(state.held_direction),
                        price=float(quote) if quote else state.entry_price,
                        pnl=pnl,
                    )
                    state.last_action = f"反向訊號，平倉 {state.held_direction} x{state.held_qty}"
                    state.held_qty = 0
                    state.held_direction = None
                    state.entry_price = 0.0
                    state.last_signal_key = signal["key"]
                    _cancel_protection_order_for_state(state, svc)
            else:
                state.last_action = "空手，反向訊號不動作（本策略不做這個方向）"
```

改成（只在 `if state.held_qty > 0:` 內側多包一層 `if not state.reverse_signal_exit_enabled: ... else:`，原本的平倉邏輯本體整段往內多縮排一級、內容逐字不變）：

```python
        else:
            # 方向限定策略：出現反向訊號只平倉出場，不反手做另一邊
            if state.held_qty > 0:
                if not state.reverse_signal_exit_enabled:
                    state.last_action = (
                        f"已持倉 {state.held_direction} x{state.held_qty}，"
                        "已關閉反向訊號出場，持倉不動"
                    )
                else:
                    close_side = "sell" if state.held_direction == "long" else "buy"
                    result = svc.place_order(
                        {
                            "product_code": state.product_code,
                            "side": close_side,
                            "qty": state.held_qty,
                            "price": "M",
                            "trade_type": 2,
                            "new_close": 1,
                        }
                    )
                    order_result = result.get("order_result") or {}
                    if not order_result.get("success"):
                        state.last_signal_key = signal["key"]
                        state.consecutive_failures += 1
                        state.last_action = f"平倉失敗（{state.held_direction}）: {order_result.get('message') or '券商拒單'}"
                        if _check_orphaned_fill(
                            result.get("state") or {},
                            expected_side=("short" if close_side == "sell" else "long"),
                            qty=state.held_qty,
                        ):
                            # 2026-08-21 修正：這裡跟上面持倉停損停利那段的孤兒單處理不一致，
                            # 之前只有停止、沒有清空 held_qty——代表偵測到「其實已經平倉」
                            # 之後，下一輪還是會當作有留倉繼續處理，等於白偵測。統一改成
                            # 跟上面一樣清空。
                            state.held_qty = 0
                            state.held_direction = None
                            state.entry_price = 0.0
                            _cancel_protection_order_for_state(state, svc)
                            _stop_with_reason(
                                state,
                                "⚠ 平倉委託回報失敗，但偵測到近期有相符的真實成交紀錄，"
                                "這筆倉位可能其實已經平倉、或狀態跟我們追蹤的不一致，"
                                "已自動停止本策略，請立即人工核對！",
                            )
                    else:
                        pnl = None
                        if state.entry_price > 0 and quote:
                            pnl = _estimate_pnl_ntd(
                                contract=contract,
                                direction=str(state.held_direction),
                                entry_price=state.entry_price,
                                exit_price=float(quote),
                                qty=state.held_qty,
                            )
                            state.realized_pnl_ntd += pnl
                        _record_trade(
                            state.strategy_id,
                            kind="exit",
                            bar_time=signal["time"],
                            direction=str(state.held_direction),
                            price=float(quote) if quote else state.entry_price,
                            pnl=pnl,
                        )
                        state.last_action = f"反向訊號，平倉 {state.held_direction} x{state.held_qty}"
                        state.held_qty = 0
                        state.held_direction = None
                        state.entry_price = 0.0
                        state.last_signal_key = signal["key"]
                        _cancel_protection_order_for_state(state, svc)
            else:
                state.last_action = "空手，反向訊號不動作（本策略不做這個方向）"
```

- [ ] **Step 4: 執行測試確認通過**

Run: `python -m pytest tests/test_strategy_service.py -k reverse_signal -v`
Expected: PASS，包含 `test_tick_one_reverse_signal_closes_position_by_default`（既有，確認預設行為不變）、`test_tick_one_reverse_signal_skipped_when_disabled`（新）、`test_tick_one_reverse_signal_flat_position_unaffected_by_toggle`（新）。

- [ ] **Step 5: Commit**

```bash
git add backend/strategy_service.py tests/test_strategy_service.py
git commit -m "feat: gate Layer 4 reverse-signal exit behind reverse_signal_exit_enabled toggle

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_018YRXPuiMdg4mouz4pBsXWb"
```

---

### Task 5: `start_strategy()` 傳遞 4 個開關

**Files:**
- Modify: `backend/strategy_service.py:657-695`
- Test: `tests/test_strategy_service.py`

**Interfaces:**
- Consumes: Task 1 的 `StrategyState` 欄位、`strategy_config_defaults()`。
- Produces: `start_strategy(strategy_id, product_code, *, ..., oco_enabled=None, soft_stop_enabled=None, risk_insurance_enabled=None, reverse_signal_exit_enabled=None)`——4 個新關鍵字參數都是 `bool | None = None`，`None` 時退回 `strategy_config_defaults()` 的值（固定 `True`）。

- [ ] **Step 1: 寫失敗測試**

`tests/test_strategy_service.py:1083-1096` 已經有一個 `start_strategy` 的既有測試 `test_start_strategy_accepts_custom_risk_parameters`，只 `patch("backend.strategy_service._load_bars", ...)`、不 mock `TradingService`（`_fetch_equity_basis()` 內部呼叫失敗時已有 `try/except` 退回 `.env` 預設值，見該函式）。新測試緊接著加在它後面，沿用同一個 patch 模式：

```python
def test_start_strategy_applies_layer_toggle_overrides(clean_armed):
    from backend.strategy_service import start_strategy, _armed

    with patch("backend.strategy_service._load_bars", return_value=pd.DataFrame()):
        start_strategy(
            "breakout_long", "TM2609",
            oco_enabled=False, soft_stop_enabled=False,
            risk_insurance_enabled=True, reverse_signal_exit_enabled=False,
        )

    state = _armed["breakout_long"]
    assert state.oco_enabled is False
    assert state.soft_stop_enabled is False
    assert state.risk_insurance_enabled is True
    assert state.reverse_signal_exit_enabled is False


def test_start_strategy_defaults_all_toggles_true_when_omitted(clean_armed):
    from backend.strategy_service import start_strategy, _armed

    with patch("backend.strategy_service._load_bars", return_value=pd.DataFrame()):
        start_strategy("breakout_long", "TM2609")

    state = _armed["breakout_long"]
    assert state.oco_enabled is True
    assert state.soft_stop_enabled is True
    assert state.risk_insurance_enabled is True
    assert state.reverse_signal_exit_enabled is True
```

`start_strategy` 沒有在檔案頂部的匯入清單（第 10-23 行）裡，`_armed` 有——這裡跟緊鄰的 `test_start_strategy_accepts_custom_risk_parameters` 一樣，在函式內部用 `from backend.strategy_service import start_strategy, _armed` 局部匯入（`_armed` 重複匯入沒有副作用，只是跟既有寫法保持一致）。

- [ ] **Step 2: 執行測試確認失敗**

Run: `python -m pytest tests/test_strategy_service.py -k start_strategy_applies_layer_toggle -v`
Expected: FAIL，`TypeError: start_strategy() got an unexpected keyword argument 'oco_enabled'`。

- [ ] **Step 3: 加參數**

`backend/strategy_service.py` 第 657-667 行，原本：

```python
def start_strategy(
    strategy_id: str,
    product_code: str,
    *,
    qty: int | None = None,
    stop_loss_points: float | None = None,
    take_profit_points: float | None = None,
    initial_capital_ntd: float | None = None,
    max_loss_ntd: float | None = None,
    max_loss_pct: float | None = None,
) -> dict[str, Any]:
```

改成：

```python
def start_strategy(
    strategy_id: str,
    product_code: str,
    *,
    qty: int | None = None,
    stop_loss_points: float | None = None,
    take_profit_points: float | None = None,
    initial_capital_ntd: float | None = None,
    max_loss_ntd: float | None = None,
    max_loss_pct: float | None = None,
    oco_enabled: bool | None = None,
    soft_stop_enabled: bool | None = None,
    risk_insurance_enabled: bool | None = None,
    reverse_signal_exit_enabled: bool | None = None,
) -> dict[str, Any]:
```

同一個函式裡，`StrategyState(...)` 建構呼叫（第 671-683 行）加 4 行，`take_profit_points=...` 那行之後：

```python
        oco_enabled=bool(oco_enabled if oco_enabled is not None else defaults["oco_enabled"]),
        soft_stop_enabled=bool(soft_stop_enabled if soft_stop_enabled is not None else defaults["soft_stop_enabled"]),
        risk_insurance_enabled=bool(risk_insurance_enabled if risk_insurance_enabled is not None else defaults["risk_insurance_enabled"]),
        reverse_signal_exit_enabled=bool(reverse_signal_exit_enabled if reverse_signal_exit_enabled is not None else defaults["reverse_signal_exit_enabled"]),
```

- [ ] **Step 4: 執行測試確認通過**

Run: `python -m pytest tests/test_strategy_service.py -k start_strategy -v`
Expected: PASS（新的 2 個測試 + 既有跟 `start_strategy` 相關的測試都過）。

- [ ] **Step 5: Commit**

```bash
git add backend/strategy_service.py tests/test_strategy_service.py
git commit -m "feat: wire 4 layer toggles through start_strategy()

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_018YRXPuiMdg4mouz4pBsXWb"
```

---

### Task 6: `admin_upsert_strategy_config()` 傳遞 4 個開關

**Files:**
- Modify: `backend/strategy_config_store.py:64-99`
- Test: `tests/test_strategy_config_store.py`

**Interfaces:**
- Produces: `admin_upsert_strategy_config(..., oco_enabled: bool | None = None, soft_stop_enabled: bool | None = None, risk_insurance_enabled: bool | None = None, reverse_signal_exit_enabled: bool | None = None)`——沿用既有「非 None 才放進 payload」的寫法。

- [ ] **Step 1: 寫失敗測試**

在 `tests/test_strategy_config_store.py`，`test_admin_upsert_posts_risk_parameters` 之後加：

```python
def test_admin_upsert_posts_layer_toggles(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")

    fake_response = MagicMock()
    fake_response.json.return_value = [
        {"strategy_id": "breakout_long", "user_id": "user-456", "enabled": True}
    ]
    fake_response.raise_for_status.return_value = None

    with patch("backend.strategy_config_store.requests.post", return_value=fake_response) as mock_post:
        admin_upsert_strategy_config(
            "user-456",
            "breakout_long",
            product_code="TM2609",
            qty=1,
            enabled=True,
            oco_enabled=False,
            soft_stop_enabled=True,
            risk_insurance_enabled=False,
            reverse_signal_exit_enabled=True,
        )

    payload = mock_post.call_args.kwargs["json"]
    assert payload == {
        "user_id": "user-456",
        "strategy_id": "breakout_long",
        "product_code": "TM2609",
        "qty": 1,
        "enabled": True,
        "oco_enabled": False,
        "soft_stop_enabled": True,
        "risk_insurance_enabled": False,
        "reverse_signal_exit_enabled": True,
    }


def test_admin_upsert_omits_layer_toggles_when_not_specified(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")

    fake_response = MagicMock()
    fake_response.json.return_value = [{"strategy_id": "breakout_long"}]
    fake_response.raise_for_status.return_value = None

    with patch("backend.strategy_config_store.requests.post", return_value=fake_response) as mock_post:
        admin_upsert_strategy_config(
            "user-456", "breakout_long", product_code="TM2609", qty=1, enabled=True,
        )

    payload = mock_post.call_args.kwargs["json"]
    assert "oco_enabled" not in payload
    assert "soft_stop_enabled" not in payload
    assert "risk_insurance_enabled" not in payload
    assert "reverse_signal_exit_enabled" not in payload
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `python -m pytest tests/test_strategy_config_store.py -k layer_toggles -v`
Expected: FAIL，`TypeError: admin_upsert_strategy_config() got an unexpected keyword argument 'oco_enabled'`。

- [ ] **Step 3: 加參數**

`backend/strategy_config_store.py` 第 64-99 行，函式簽名（第 64-76 行）原本：

```python
def admin_upsert_strategy_config(
    user_id: str,
    strategy_id: str,
    *,
    product_code: str,
    qty: int | None = None,
    enabled: bool = False,
    stop_loss_points: float | None = None,
    take_profit_points: float | None = None,
    max_loss_ntd: float | None = None,
    max_loss_pct: float | None = None,
    timeout: float = 10.0,
) -> dict:
```

改成：

```python
def admin_upsert_strategy_config(
    user_id: str,
    strategy_id: str,
    *,
    product_code: str,
    qty: int | None = None,
    enabled: bool = False,
    stop_loss_points: float | None = None,
    take_profit_points: float | None = None,
    max_loss_ntd: float | None = None,
    max_loss_pct: float | None = None,
    oco_enabled: bool | None = None,
    soft_stop_enabled: bool | None = None,
    risk_insurance_enabled: bool | None = None,
    reverse_signal_exit_enabled: bool | None = None,
    timeout: float = 10.0,
) -> dict:
```

函式內部（第 92-99 行）原本：

```python
    if stop_loss_points is not None:
        payload["stop_loss_points"] = float(stop_loss_points)
    if take_profit_points is not None:
        payload["take_profit_points"] = float(take_profit_points)
    if max_loss_ntd is not None:
        payload["max_loss_ntd"] = float(max_loss_ntd)
    if max_loss_pct is not None:
        payload["max_loss_pct"] = float(max_loss_pct)
```

之後加：

```python
    if oco_enabled is not None:
        payload["oco_enabled"] = bool(oco_enabled)
    if soft_stop_enabled is not None:
        payload["soft_stop_enabled"] = bool(soft_stop_enabled)
    if risk_insurance_enabled is not None:
        payload["risk_insurance_enabled"] = bool(risk_insurance_enabled)
    if reverse_signal_exit_enabled is not None:
        payload["reverse_signal_exit_enabled"] = bool(reverse_signal_exit_enabled)
```

- [ ] **Step 4: 執行測試確認通過**

Run: `python -m pytest tests/test_strategy_config_store.py -v`
Expected: PASS（全部，含既有測試沒有回歸）。

- [ ] **Step 5: Commit**

```bash
git add backend/strategy_config_store.py tests/test_strategy_config_store.py
git commit -m "feat: wire 4 layer toggles through admin_upsert_strategy_config()

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_018YRXPuiMdg4mouz4pBsXWb"
```

---

### Task 7: `_auto_arm_bound_strategies()` 傳遞 4 個開關

**Files:**
- Modify: `backend/api.py:97-120`
- Test: `tests/test_api_worker_auto_arm.py`

**Interfaces:**
- Consumes: Task 5 的 `start_strategy(..., oco_enabled=..., ...)`。

- [ ] **Step 1: 寫失敗測試**

修改 `tests/test_api_worker_auto_arm.py` 的 `test_arms_only_enabled_bound_strategies`：在 `rows[0]` 那個 dict 裡加上 4 個開關 key，並在 `mock_start.assert_called_once_with(...)` 的期望參數裡也加上：

原本：

```python
    rows = [
        {
            "strategy_id": "breakout_long",
            "product_code": "TM2609",
            "qty": 2,
            "enabled": True,
            "stop_loss_points": 80.0,
            "take_profit_points": 200.0,
            "max_loss_ntd": 5000.0,
            "max_loss_pct": 0.05,
        },
        {"strategy_id": "breakout_short", "product_code": "TM2609", "qty": 1, "enabled": False},
    ]
    with patch("backend.api.list_user_strategy_configs", return_value=rows) as mock_list, \
         patch("backend.api.start_strategy") as mock_start:
        _auto_arm_bound_strategies()

    mock_list.assert_called_once_with("user-123")
    mock_start.assert_called_once_with(
        "breakout_long",
        "TM2609",
        qty=2,
        stop_loss_points=80.0,
        take_profit_points=200.0,
        max_loss_ntd=5000.0,
        max_loss_pct=0.05,
    )
```

改成：

```python
    rows = [
        {
            "strategy_id": "breakout_long",
            "product_code": "TM2609",
            "qty": 2,
            "enabled": True,
            "stop_loss_points": 80.0,
            "take_profit_points": 200.0,
            "max_loss_ntd": 5000.0,
            "max_loss_pct": 0.05,
            "oco_enabled": False,
            "soft_stop_enabled": True,
            "risk_insurance_enabled": True,
            "reverse_signal_exit_enabled": False,
        },
        {"strategy_id": "breakout_short", "product_code": "TM2609", "qty": 1, "enabled": False},
    ]
    with patch("backend.api.list_user_strategy_configs", return_value=rows) as mock_list, \
         patch("backend.api.start_strategy") as mock_start:
        _auto_arm_bound_strategies()

    mock_list.assert_called_once_with("user-123")
    mock_start.assert_called_once_with(
        "breakout_long",
        "TM2609",
        qty=2,
        stop_loss_points=80.0,
        take_profit_points=200.0,
        max_loss_ntd=5000.0,
        max_loss_pct=0.05,
        oco_enabled=False,
        soft_stop_enabled=True,
        risk_insurance_enabled=True,
        reverse_signal_exit_enabled=False,
    )
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `python -m pytest tests/test_api_worker_auto_arm.py -v`
Expected: FAIL，`mock_start.assert_called_once_with(...)` 因為實際呼叫少了 4 個 kwargs 而不相符。

- [ ] **Step 3: 加傳遞**

`backend/api.py` 第 112-120 行，原本：

```python
        start_strategy(
            row["strategy_id"],
            row["product_code"],
            qty=row.get("qty"),
            stop_loss_points=row.get("stop_loss_points"),
            take_profit_points=row.get("take_profit_points"),
            max_loss_ntd=row.get("max_loss_ntd"),
            max_loss_pct=row.get("max_loss_pct"),
        )
```

改成：

```python
        start_strategy(
            row["strategy_id"],
            row["product_code"],
            qty=row.get("qty"),
            stop_loss_points=row.get("stop_loss_points"),
            take_profit_points=row.get("take_profit_points"),
            max_loss_ntd=row.get("max_loss_ntd"),
            max_loss_pct=row.get("max_loss_pct"),
            oco_enabled=row.get("oco_enabled"),
            soft_stop_enabled=row.get("soft_stop_enabled"),
            risk_insurance_enabled=row.get("risk_insurance_enabled"),
            reverse_signal_exit_enabled=row.get("reverse_signal_exit_enabled"),
        )
```

- [ ] **Step 4: 執行測試確認通過**

Run: `python -m pytest tests/test_api_worker_auto_arm.py -v`
Expected: PASS（全部 3 個測試）。

- [ ] **Step 5: Commit**

```bash
git add backend/api.py tests/test_api_worker_auto_arm.py
git commit -m "feat: pass 4 layer toggles through _auto_arm_bound_strategies()

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_018YRXPuiMdg4mouz4pBsXWb"
```

---

### Task 8: `StrategyStartRequest` 與 `/api/strategy/start` 傳遞 4 個開關

**Files:**
- Modify: `backend/api.py:240-247`（`StrategyStartRequest`）、`backend/api.py:563-576`（`live_strategy_start`）
- Test: `tests/test_api_strategy_config.py`

**Interfaces:**
- Consumes: Task 5 的 `start_strategy(..., oco_enabled=..., ...)`。

- [ ] **Step 1: 寫失敗測試**

在 `tests/test_api_strategy_config.py` 加（需要新 import `StrategyStartRequest`、`live_strategy_start`）：

```python
from backend.api import StrategyStartRequest, api_me, api_my_strategy_configs, live_strategy_start


def test_live_strategy_start_passes_layer_toggles_through():
    request = StrategyStartRequest(
        strategy_id="breakout_long",
        product_code="TM2609",
        oco_enabled=False,
        soft_stop_enabled=True,
        risk_insurance_enabled=False,
        reverse_signal_exit_enabled=True,
    )

    with patch("backend.api.start_strategy", return_value={"strategy_id": "breakout_long"}) as mock_start:
        live_strategy_start(request)

    mock_start.assert_called_once_with(
        "breakout_long",
        "TM2609",
        qty=None,
        stop_loss_points=None,
        take_profit_points=None,
        max_loss_ntd=None,
        max_loss_pct=None,
        oco_enabled=False,
        soft_stop_enabled=True,
        risk_insurance_enabled=False,
        reverse_signal_exit_enabled=True,
    )


def test_strategy_start_request_defaults_layer_toggles_to_none():
    request = StrategyStartRequest(strategy_id="breakout_long", product_code="TM2609")
    assert request.oco_enabled is None
    assert request.soft_stop_enabled is None
    assert request.risk_insurance_enabled is None
    assert request.reverse_signal_exit_enabled is None
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `python -m pytest tests/test_api_strategy_config.py -k layer_toggle -v`
Expected: FAIL，`pydantic.ValidationError` 或 `TypeError`（`StrategyStartRequest` 還沒有這些欄位）。

- [ ] **Step 3: 加欄位與傳遞**

`backend/api.py` 第 240-247 行，原本：

```python
class StrategyStartRequest(BaseModel):
    strategy_id: str
    product_code: str = "TM2608"
    qty: int | None = None
    stop_loss_points: float | None = None
    take_profit_points: float | None = None
    max_loss_ntd: float | None = None
    max_loss_pct: float | None = None
```

改成：

```python
class StrategyStartRequest(BaseModel):
    strategy_id: str
    product_code: str = "TM2608"
    qty: int | None = None
    stop_loss_points: float | None = None
    take_profit_points: float | None = None
    max_loss_ntd: float | None = None
    max_loss_pct: float | None = None
    oco_enabled: bool | None = None
    soft_stop_enabled: bool | None = None
    risk_insurance_enabled: bool | None = None
    reverse_signal_exit_enabled: bool | None = None
```

第 563-576 行，原本：

```python
@app.post("/api/strategy/start")
def live_strategy_start(request: StrategyStartRequest) -> dict[str, object]:
    try:
        return start_strategy(
            request.strategy_id,
            request.product_code,
            qty=request.qty,
            stop_loss_points=request.stop_loss_points,
            take_profit_points=request.take_profit_points,
            max_loss_ntd=request.max_loss_ntd,
            max_loss_pct=request.max_loss_pct,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
```

改成：

```python
@app.post("/api/strategy/start")
def live_strategy_start(request: StrategyStartRequest) -> dict[str, object]:
    try:
        return start_strategy(
            request.strategy_id,
            request.product_code,
            qty=request.qty,
            stop_loss_points=request.stop_loss_points,
            take_profit_points=request.take_profit_points,
            max_loss_ntd=request.max_loss_ntd,
            max_loss_pct=request.max_loss_pct,
            oco_enabled=request.oco_enabled,
            soft_stop_enabled=request.soft_stop_enabled,
            risk_insurance_enabled=request.risk_insurance_enabled,
            reverse_signal_exit_enabled=request.reverse_signal_exit_enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
```

- [ ] **Step 4: 執行測試確認通過**

Run: `python -m pytest tests/test_api_strategy_config.py -v`
Expected: PASS（全部）。

- [ ] **Step 5: Commit**

```bash
git add backend/api.py tests/test_api_strategy_config.py
git commit -m "feat: expose 4 layer toggles on StrategyStartRequest / /api/strategy/start

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_018YRXPuiMdg4mouz4pBsXWb"
```

---

### Task 9: `scripts/bind_strategy.py` CLI 新增 4 個開關 flag

**Files:**
- Modify: `scripts/bind_strategy.py:51-89`

**Interfaces:**
- Consumes: Task 6 的 `admin_upsert_strategy_config(..., oco_enabled=..., ...)`。

這支腳本目前沒有自動化測試（是給部署時手動執行的工程操作腳本，見腳本開頭說明），本次也不新增測試框架——用手動執行 `--help` 驗證取代，符合現有專案模式（不引入新測試工具）。

- [ ] **Step 1: 加 4 個 CLI flag**

`scripts/bind_strategy.py` 第 61-62 行，原本：

```python
    parser.add_argument("--max-loss-pct", type=float, default=None, help="客製化比例硬停損（如 0.10）")
    parser.add_argument("--enabled", action="store_true", help="建立後直接標記為啟用（預設不啟用）")
```

改成：

```python
    parser.add_argument("--max-loss-pct", type=float, default=None, help="客製化比例硬停損（如 0.10）")
    parser.add_argument(
        "--oco-enabled", action=argparse.BooleanOptionalAction, default=None,
        help="Layer 1 券商 OCO 智慧單開關（不指定則沿用資料庫預設值 True）",
    )
    parser.add_argument(
        "--soft-stop-enabled", action=argparse.BooleanOptionalAction, default=None,
        help="Layer 2 本地軟停損停利備援開關（不指定則沿用資料庫預設值 True）",
    )
    parser.add_argument(
        "--risk-insurance-enabled", action=argparse.BooleanOptionalAction, default=None,
        help="Layer 3 金額/比例硬停損保險開關（不指定則沿用資料庫預設值 True）",
    )
    parser.add_argument(
        "--reverse-signal-exit-enabled", action=argparse.BooleanOptionalAction, default=None,
        help="Layer 4 反向訊號出場開關（不指定則沿用資料庫預設值 True）",
    )
    parser.add_argument("--enabled", action="store_true", help="建立後直接標記為啟用（預設不啟用）")
```

第 77-87 行，`admin_upsert_strategy_config(...)` 呼叫，原本：

```python
    row = admin_upsert_strategy_config(
        args.user_id,
        args.strategy_id,
        product_code=args.product_code,
        qty=args.qty,
        enabled=args.enabled,
        stop_loss_points=args.stop_loss_points,
        take_profit_points=args.take_profit_points,
        max_loss_ntd=args.max_loss_ntd,
        max_loss_pct=args.max_loss_pct,
    )
```

改成：

```python
    row = admin_upsert_strategy_config(
        args.user_id,
        args.strategy_id,
        product_code=args.product_code,
        qty=args.qty,
        enabled=args.enabled,
        stop_loss_points=args.stop_loss_points,
        take_profit_points=args.take_profit_points,
        max_loss_ntd=args.max_loss_ntd,
        max_loss_pct=args.max_loss_pct,
        oco_enabled=args.oco_enabled,
        soft_stop_enabled=args.soft_stop_enabled,
        risk_insurance_enabled=args.risk_insurance_enabled,
        reverse_signal_exit_enabled=args.reverse_signal_exit_enabled,
    )
```

- [ ] **Step 2: 手動驗證**

Run: `python scripts/bind_strategy.py --help`
Expected: 輸出裡看得到 `--oco-enabled`/`--no-oco-enabled`、`--soft-stop-enabled`/`--no-soft-stop-enabled`、`--risk-insurance-enabled`/`--no-risk-insurance-enabled`、`--reverse-signal-exit-enabled`/`--no-reverse-signal-exit-enabled`，指令正常結束（exit code 0），不需要連線 Supabase。

再跑一次確認既有功能沒壞：

Run: `python scripts/bind_strategy.py --list-strategies`
Expected: 正常列出可綁定的 strategy_id 清單，不需要連線 Supabase，不受這次改動影響。

- [ ] **Step 3: Commit**

```bash
git add scripts/bind_strategy.py
git commit -m "feat: add 4 layer-toggle CLI flags to bind_strategy.py

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_018YRXPuiMdg4mouz4pBsXWb"
```

---

### Task 10: SQL migration — `user_strategy_configs` 新增 4 個開關欄位

**Files:**
- Create: `supabase/sql/2026-08-31_add_layer_toggles_to_user_strategy_configs.sql`

**Interfaces:**
- Produces: `public.user_strategy_configs` 表多 4 個欄位 `oco_enabled`/`soft_stop_enabled`/`risk_insurance_enabled`/`reverse_signal_exit_enabled`，型別 `boolean not null default true`。

- [ ] **Step 1: 寫 migration 檔**

```sql
-- supabase/sql/2026-08-31_add_layer_toggles_to_user_strategy_configs.sql
-- 為 user_strategy_configs 表新增四道出場防護網的獨立開關欄位，可依帳號/
-- 策略客製化關閉；not null default true 確保既有資料列與未來新插入的列，
-- 沒指定值時一律視為「開啟」，跟 StrategyState 的 Python 端預設值一致。
alter table if exists public.user_strategy_configs
  add column if not exists oco_enabled boolean not null default true,
  add column if not exists soft_stop_enabled boolean not null default true,
  add column if not exists risk_insurance_enabled boolean not null default true,
  add column if not exists reverse_signal_exit_enabled boolean not null default true;
```

- [ ] **Step 2: 人工確認**

這個檔案本身不會自動套用到真實 Supabase 資料庫（跟 2026-08-31 稍早那份 `2026-08-31_add_risk_params_to_user_strategy_configs.sql` 一樣，需要人工在 Supabase SQL editor 或 CLI 手動執行一次）。**這一步先不要自己執行**——寫完檔案後明確告訴使用者：「這份 migration 需要你手動到 Supabase 專案的 SQL editor 執行一次，我不會自己連線跑」，並確認前一份風控參數的 migration（`2026-08-31_add_risk_params_to_user_strategy_configs.sql`）是否已經跑過，避免累積兩份沒套用的 migration。

- [ ] **Step 3: Commit**

```bash
git add supabase/sql/2026-08-31_add_layer_toggles_to_user_strategy_configs.sql
git commit -m "docs: add SQL migration for 4 layer-toggle columns on user_strategy_configs

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_018YRXPuiMdg4mouz4pBsXWb"
```

---

### Task 11: `docs/live_trading_flow.md` 補充開關說明與風險註記

**Files:**
- Modify: `docs/live_trading_flow.md:156-167`

**Interfaces:** 無（純文件）。

- [ ] **Step 1: 在「4 道防線詳細規格」之後加一段新小節**

`docs/live_trading_flow.md` 第 166-168 行，原本：

```markdown
4. **第 4 道：60 分 K 反向訊號平倉（趨勢翻轉出場）**
   - 當 60 分 K 收盤出現相反方向訊號時，若手上有多單則市價賣出平倉（空單則市價買進平倉），**只平倉、不反手**。

---
```

改成：

```markdown
4. **第 4 道：60 分 K 反向訊號平倉（趨勢翻轉出場）**
   - 當 60 分 K 收盤出現相反方向訊號時，若手上有多單則市價賣出平倉（空單則市價買進平倉），**只平倉、不反手**。

### 各層獨立開關（2026-08-31）

四道防線各自對應 `StrategyState` 的一個獨立布林欄位（`oco_enabled`／
`soft_stop_enabled`／`risk_insurance_enabled`／`reverse_signal_exit_enabled`），
可透過 `user_strategy_configs` 表依帳號/策略客製化關閉，預設全部開啟。四層
**完全對等**，沒有任何一層是強制、不可關閉的——包含第 3 道風控保險網也可以
關掉。

⚠️ **風險**：四層全部關閉是系統允許的合法狀態，代表那筆倉位完全沒有任何
自動出場保護，只能靠人工到券商 App 或 Dashboard 手動平倉。這不是建議用法，
是刻意保留給有特殊需求的帳號的彈性，設定客製化開關前務必想清楚會不會不小心
關掉所有防護。

---
```

- [ ] **Step 2: Commit**

```bash
git add docs/live_trading_flow.md
git commit -m "docs: document 4 independent layer toggles in live trading flow doc

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_018YRXPuiMdg4mouz4pBsXWb"
```

---

### Task 12: 全套測試確認無回歸

**Files:** 無新增/修改，純驗證。

- [ ] **Step 1: 跑全套測試**

Run: `python -m pytest tests/ -q`
Expected: 全部 PASS，沒有 FAIL/ERROR。若有既有測試因為這次改動意外壞掉（例如某個直接用位置參數呼叫 `StrategyState(...)` 或 `start_strategy(...)` 的測試，因為新增的關鍵字參數改變了什麼），回到對應 Task 修正，不要在這裡用不相關的改動硬湊過關。

- [ ] **Step 2: 確認 git log 乾淨**

Run: `git log --oneline -13`
Expected: 看得到 Task 1-11 這 11 個 commit（Task 10 有 2 個 commit-worthy 動作但只 1 個 commit，其餘每個 Task 1 個 commit），訊息清楚對應這次的工作內容。

- [ ] **Step 3: 回報使用者尚待人工處理的項目**

跟使用者明確列出這個計畫**不包含**、仍需要人工處理的事：
1. Task 10 的 SQL migration 需要手動到 Supabase SQL editor 執行。
2. 這次沒有做 Dashboard 前端的開關按鈕 UI（`_state_to_dict()` 已經回傳這 4 個欄位，前端要顯示/切換時資料已經在，但實際按鈕畫面本次不做）。
3. VM 上的部署（重建 Setup.exe / 重啟 worker 行程）需要另外執行，這個計畫只改本地程式碼。
