# 15分鐘/60分鐘 圖表切換 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the Dashboard chart display either 15-minute or 60-minute bars (with matching MA lines and, on 15-minute, death-cross signal data), so the death-cross strategy is actually visible instead of only showing up as trade-execution arrows on a 60-minute background.

**Architecture:** Thread an `interval_minutes`/`interval` parameter through the already-existing 15-minute infrastructure (`get_kline_store`, `generate_death_cross_signals`, both built in the earlier live-trading phase) at 4 backend call sites, all defaulting to 60 so nothing existing changes behavior. Add a frontend toggle that re-fetches and redraws on click, and makes the live WebSocket tick handler interval-aware.

Spec: `docs/superpowers/specs/2026-09-29-death-cross-15min-chart-toggle-design.md`.

## Global Constraints

- Every new parameter added in this plan defaults to `60` (or is only reached when `interval_minutes != 60`), so no existing caller's behavior changes.
- `/api/klines/signals` response rows only gain a `death_cross_signal` key when `interval=15`; the `interval=60` (default) response shape is byte-for-byte unchanged from today, so nothing already parsing this endpoint's rows breaks on an unexpected key.
- No new time intervals beyond 15/60 — this is not a general-purpose timeframe picker.
- `frontend/js/app.js:360`'s `updateLiveMA(msg.kline)` call must also become interval-aware (use the same `liveKline` the candle series update uses) — a design-review finding, not optional polish.
- `frontend/index.html:146`'s `<script src="js/app.js?v=67"></script>` must be bumped to `v=68` in the same commit that changes `app.js`, so browsers don't serve a cached pre-toggle copy.

---

## Task 1: Backend — `interval` parameter through 4 call sites

**Files:**
- Modify: `backend/live_service.py` (import line ~28, `get_klines()` at line 269-271, `poll_once()` at lines 383-394)
- Modify: `backend/api.py` (`/api/klines` at line 573-580, `/api/klines/signals` at line 583-589)
- Modify: `backend/strategy_service.py` (`klines_with_signals()` at lines 666-700)
- Test: `tests/test_live_service_klines.py` (new file), `tests/test_strategy_service.py` (append), `tests/test_api_klines_interval.py` (new file)

**Interfaces:**
- Consumes: `get_kline_store(product_code, interval_minutes)` (`backend/kline_engine.py`, already exists), `_load_bars(..., interval_minutes=, min_bars_required=)` and `_interval_minutes_for_strategy`/`_min_bars_required_for_strategy` (`backend/strategy_service.py`, already exist), `generate_death_cross_signals` via `_signaled_frame("death_cross", bars)` (already exists).
- Produces: `get_klines(product, limit=500, interval_minutes=60)`; `klines_with_signals(product_code, limit=500, interval_minutes=60)` — rows gain a `"death_cross_signal"` key only when `interval_minutes != 60`; `/api/klines?product=...&interval=60` and `/api/klines/signals?product=...&interval=60` query params; `poll_once()`'s returned dict gains a `"kline_15m"` key alongside the existing `"kline"`.

- [ ] **Step 1: Write the failing tests**

`tests/test_live_service_klines.py` (new file):
```python
from unittest.mock import MagicMock, patch

import pytest

from backend import live_service


def test_get_klines_default_uses_60_minute_store(monkeypatch):
    calls = []

    def _fake_get_kline_store(code, interval_minutes=60):
        calls.append(interval_minutes)
        store = MagicMock()
        store.get_klines.return_value = [{"time": 1, "close": 1.0}]
        return store

    monkeypatch.setattr(live_service, "get_kline_store", _fake_get_kline_store)
    monkeypatch.setattr(live_service, "resolve_quote_product_code", lambda p: "TM2609")

    result = live_service.get_klines("TM2609")

    assert calls == [60]
    assert result == [{"time": 1, "close": 1.0}]


def test_get_klines_15min_uses_15_minute_store(monkeypatch):
    calls = []

    def _fake_get_kline_store(code, interval_minutes=60):
        calls.append(interval_minutes)
        store = MagicMock()
        store.get_klines.return_value = [{"time": 2, "close": 2.0}]
        return store

    monkeypatch.setattr(live_service, "get_kline_store", _fake_get_kline_store)
    monkeypatch.setattr(live_service, "resolve_quote_product_code", lambda p: "TM2609")

    result = live_service.get_klines("TM2609", interval_minutes=15)

    assert calls == [15]
    assert result == [{"time": 2, "close": 2.0}]


def _base_status(*, subscribed_product: str) -> dict:
    return {
        "connected": True,
        "subscribed_product": subscribed_product,
        "quote": {"product_code": subscribed_product, "last_price": None},
        "quote_ready": False,
        "quote_connected": 1,
        "kline_loaded_products": [subscribed_product],
    }


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_poll_once_broadcasts_both_60min_and_15min_klines():
    mock_svc = MagicMock()
    mock_svc.status.return_value = _base_status(subscribed_product="TM2610")

    def _fake_get_klines(product, limit=1, interval_minutes=60):
        if interval_minutes == 15:
            return [{"time": 1000, "close": 15.0}]
        return [{"time": 2000, "close": 60.0}]

    with patch("backend.live_service._svc", return_value=mock_svc), \
         patch("backend.strategy_service.resolve_strategy_product_code", return_value="TM2610"), \
         patch("backend.live_service._should_attempt_periodic_shioaji_backfill", return_value=False), \
         patch("backend.live_service.get_klines", side_effect=_fake_get_klines):
        tick = await live_service.poll_once()

    assert tick["kline"]["close"] == 60.0
    assert tick["kline_15m"]["close"] == 15.0
```

Append to `tests/test_strategy_service.py` (add `klines_with_signals` to the existing `from backend.strategy_service import (...)` block):
```python
def test_klines_with_signals_60min_default_has_no_death_cross_field():
    fake_bars = pd.DataFrame(
        {
            "open": [100.0], "high": [100.0], "low": [100.0], "close": [100.0],
            "ma_fast": [100.0], "ma_mid": [100.0], "ma_slow": [100.0],
        },
        index=pd.date_range("2024-01-02 09:45:00", periods=1, freq="60min"),
    )

    with patch.object(strategy_service, "_load_bars", return_value=fake_bars) as mock_load, \
         patch.object(strategy_service, "_signaled_frame", return_value=fake_bars.assign(signal=[None])):
        rows = klines_with_signals("TM2609")

    assert mock_load.call_args.kwargs["interval_minutes"] == 60
    assert len(rows) == 1
    assert "death_cross_signal" not in rows[0]


def test_klines_with_signals_15min_includes_death_cross_field():
    fake_bars = pd.DataFrame(
        {
            "open": [100.0], "high": [100.0], "low": [100.0], "close": [100.0],
            "ma_fast": [100.0], "ma_mid": [100.0], "ma_slow": [100.0],
        },
        index=pd.date_range("2024-01-02 09:00:00", periods=1, freq="15min"),
    )

    with patch.object(strategy_service, "_load_bars", return_value=fake_bars) as mock_load, \
         patch.object(strategy_service, "_signaled_frame", return_value=fake_bars.assign(signal=[None])):
        rows = klines_with_signals("TM2609", interval_minutes=15)

    assert mock_load.call_args.kwargs["interval_minutes"] == 15
    assert mock_load.call_args.kwargs["min_bars_required"] == 22
    assert len(rows) == 1
    assert "death_cross_signal" in rows[0]
```

`tests/test_api_klines_interval.py` (new file — **important**: `live_klines`/`live_klines_signals` import `resolve_strategy_product_code` *locally inside the function body*, not at module level, so it must be patched at its source module `backend.strategy_service`, not as `backend.api.resolve_strategy_product_code`, which does not exist):
```python
from unittest.mock import patch

from backend.api import live_klines, live_klines_signals


def test_live_klines_default_interval_is_60():
    with patch("backend.strategy_service.resolve_strategy_product_code", return_value="TM2609"), \
         patch("backend.api.get_klines", return_value=[]) as mock_get_klines:
        live_klines(product="TMF", limit=500)

    assert mock_get_klines.call_args.kwargs["interval_minutes"] == 60


def test_live_klines_passes_interval_15_to_get_klines():
    with patch("backend.strategy_service.resolve_strategy_product_code", return_value="TM2609"), \
         patch("backend.api.get_klines", return_value=[]) as mock_get_klines:
        live_klines(product="TMF", limit=500, interval=15)

    assert mock_get_klines.call_args.kwargs["interval_minutes"] == 15


def test_live_klines_signals_passes_interval_15_to_klines_with_signals():
    with patch("backend.strategy_service.resolve_strategy_product_code", return_value="TM2609"), \
         patch("backend.api.klines_with_signals", return_value=[]) as mock_kws:
        live_klines_signals(product="TMF", limit=500, interval=15)

    assert mock_kws.call_args.kwargs["interval_minutes"] == 15
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_live_service_klines.py tests/test_api_klines_interval.py tests/test_strategy_service.py -k "klines" -v`
Expected: FAIL — `get_klines()`/`klines_with_signals()`/`live_klines`/`live_klines_signals` don't accept `interval_minutes`/`interval` yet; `poll_once()` doesn't return `kline_15m`.

- [ ] **Step 3: Implement**

3a. `backend/live_service.py` — import line, change:
```python
from .kline_engine import DEFAULT_KLINE_LIMIT, get_store
```
to:
```python
from .kline_engine import DEFAULT_KLINE_LIMIT, get_kline_store, get_store
```
(`get_store` stays — it's still used elsewhere in this file, e.g. line 180.)

3b. `get_klines()` — change:
```python
def get_klines(product: str | None = None, limit: int = DEFAULT_KLINE_LIMIT) -> list[dict[str, Any]]:
    code = resolve_quote_product_code(product or current_product)
    return get_store(code).get_klines(limit=limit)
```
to:
```python
def get_klines(
    product: str | None = None, limit: int = DEFAULT_KLINE_LIMIT, interval_minutes: int = 60
) -> list[dict[str, Any]]:
    code = resolve_quote_product_code(product or current_product)
    return get_kline_store(code, interval_minutes=interval_minutes).get_klines(limit=limit)
```

3c. `poll_once()` — change:
```python
    klines = get_klines(quote_product, limit=1)
    kline = klines[-1] if klines else None

    return {
        "type": "tick",
        "product_code": product,
        "price": last,
        "bid": q.get("bid"),
        "ask": q.get("ask"),
        "kline": kline,
        "connected": True,
    }
```
to:
```python
    klines = get_klines(quote_product, limit=1)
    kline = klines[-1] if klines else None
    klines_15m = get_klines(quote_product, limit=1, interval_minutes=15)
    kline_15m = klines_15m[-1] if klines_15m else None

    return {
        "type": "tick",
        "product_code": product,
        "price": last,
        "bid": q.get("bid"),
        "ask": q.get("ask"),
        "kline": kline,
        "kline_15m": kline_15m,
        "connected": True,
    }
```

3d. `backend/strategy_service.py::klines_with_signals()` — change:
```python
def klines_with_signals(product_code: str, limit: int = 500) -> list[dict[str, Any]]:
    """給圖表用：K 棒 + MA(5/20/60) + 突破/回測兩套策略各自的訊號欄位。"""
    contract = _contract_from_product(product_code)
    bars = _load_bars(contract, product_code=product_code)
    if bars.empty:
        return []
    bars = bars.tail(limit)

    breakout = _signaled_frame("breakout", bars)
    pullback = _signaled_frame("pullback", bars)

    def _num(value: Any) -> float | None:
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        return None if pd.isna(f) else f

    rows: list[dict[str, Any]] = []
    for i, (idx, row) in enumerate(bars.iterrows()):
        rows.append(
            {
                "time": to_unix_seconds(idx),
                "open": _num(row.get("open")),
                "high": _num(row.get("high")),
                "low": _num(row.get("low")),
                "close": _num(row.get("close")),
                "ma_fast": _num(row.get("ma_fast")),
                "ma_mid": _num(row.get("ma_mid")),
                "ma_slow": _num(row.get("ma_slow")),
                "breakout_signal": breakout.iloc[i].get("signal") if i < len(breakout) else None,
                "pullback_signal": pullback.iloc[i].get("signal") if i < len(pullback) else None,
            }
        )
    return rows
```
to:
```python
def klines_with_signals(
    product_code: str, limit: int = 500, interval_minutes: int = 60
) -> list[dict[str, Any]]:
    """給圖表用：K 棒 + MA(5/20/60) + 各策略訊號欄位。interval_minutes=60（預設）
    只算突破/回測；interval_minutes=15 額外算死叉訊號。"""
    contract = _contract_from_product(product_code)
    min_bars_required = (
        _min_bars_required_for_strategy("death_cross") if interval_minutes != 60 else None
    )
    bars = _load_bars(
        contract,
        product_code=product_code,
        interval_minutes=interval_minutes,
        min_bars_required=min_bars_required,
    )
    if bars.empty:
        return []
    bars = bars.tail(limit)

    breakout = _signaled_frame("breakout", bars)
    pullback = _signaled_frame("pullback", bars)
    death_cross = _signaled_frame("death_cross", bars) if interval_minutes != 60 else None

    def _num(value: Any) -> float | None:
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        return None if pd.isna(f) else f

    rows: list[dict[str, Any]] = []
    for i, (idx, row) in enumerate(bars.iterrows()):
        row_dict: dict[str, Any] = {
            "time": to_unix_seconds(idx),
            "open": _num(row.get("open")),
            "high": _num(row.get("high")),
            "low": _num(row.get("low")),
            "close": _num(row.get("close")),
            "ma_fast": _num(row.get("ma_fast")),
            "ma_mid": _num(row.get("ma_mid")),
            "ma_slow": _num(row.get("ma_slow")),
            "breakout_signal": breakout.iloc[i].get("signal") if i < len(breakout) else None,
            "pullback_signal": pullback.iloc[i].get("signal") if i < len(pullback) else None,
        }
        if death_cross is not None:
            row_dict["death_cross_signal"] = (
                death_cross.iloc[i].get("signal") if i < len(death_cross) else None
            )
        rows.append(row_dict)
    return rows
```
(`_min_bars_required_for_strategy` and `_interval_minutes_for_strategy` already exist in this file from the earlier live-trading phase — no new helper needed.)

3e. `backend/api.py` — change:
```python
@app.get("/api/klines")
def live_klines(product: str = "", limit: int = 500) -> list[dict[str, object]]:
    from .kline_engine import DEFAULT_KLINE_LIMIT
    from .strategy_service import resolve_strategy_product_code

    prod = resolve_strategy_product_code(product)
    effective = limit if limit > 0 else DEFAULT_KLINE_LIMIT
    return get_klines(prod, limit=min(effective, 2000))


@app.get("/api/klines/signals")
def live_klines_signals(product: str = "", limit: int = 500) -> list[dict[str, object]]:
    """K 棒 + MA(5/20/60) + 突破/回測策略訊號，給圖表疊圖用。"""
    from .strategy_service import resolve_strategy_product_code

    prod = resolve_strategy_product_code(product)
    return klines_with_signals(prod, limit=min(limit, 2000))
```
to:
```python
@app.get("/api/klines")
def live_klines(product: str = "", limit: int = 500, interval: int = 60) -> list[dict[str, object]]:
    from .kline_engine import DEFAULT_KLINE_LIMIT
    from .strategy_service import resolve_strategy_product_code

    prod = resolve_strategy_product_code(product)
    effective = limit if limit > 0 else DEFAULT_KLINE_LIMIT
    return get_klines(prod, limit=min(effective, 2000), interval_minutes=interval)


@app.get("/api/klines/signals")
def live_klines_signals(product: str = "", limit: int = 500, interval: int = 60) -> list[dict[str, object]]:
    """K 棒 + MA(5/20/60) + 各策略訊號，給圖表疊圖用。interval=15 時額外含死叉訊號。"""
    from .strategy_service import resolve_strategy_product_code

    prod = resolve_strategy_product_code(product)
    return klines_with_signals(prod, limit=min(limit, 2000), interval_minutes=interval)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_live_service_klines.py tests/test_api_klines_interval.py tests/test_strategy_service.py -k "klines" -v`
Expected: 8 passed (3 in test_live_service_klines.py, 2 in test_strategy_service.py, 3 in test_api_klines_interval.py).

- [ ] **Step 5: Run the full test suite**

Run: `python -m pytest tests/ -q`
Expected: all tests pass (existing count + 8 new).

- [ ] **Step 6: Commit**

```bash
git add backend/live_service.py backend/api.py backend/strategy_service.py tests/test_live_service_klines.py tests/test_api_klines_interval.py tests/test_strategy_service.py
git commit -m "$(cat <<'EOF'
feat(api): thread interval_minutes through klines endpoints and poll_once

get_klines()/klines_with_signals() gain an interval_minutes=60 parameter
(default preserves current behavior exactly); /api/klines and
/api/klines/signals gain a matching interval=60 query param;
klines_with_signals adds a death_cross_signal row field only when
interval_minutes=15; poll_once()'s WebSocket tick payload now includes
kline_15m alongside the existing kline. All four sites reuse existing
get_kline_store/generate_death_cross_signals infrastructure from the
earlier live-trading phase — no new backend machinery.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017ykxiou4TVNu3Q1mXgBLVg
EOF
)"
```

---

## Task 2: Frontend — interval toggle and live-update wiring

**Files:**
- Modify: `frontend/index.html` (chart panel around line 34, script tag at line 146)
- Modify: `frontend/css/style.css` (near the `.order-tab` rules at line 310-316)
- Modify: `frontend/js/app.js` (global state near line 41, `loadKlines()` at line 234-244, `loadSignals()` at line 262-266, `ws.onmessage` at line 357-362)

**Interfaces:**
- Consumes: `/api/klines?product=...&limit=...&interval=...`, `/api/klines/signals?product=...&limit=...&interval=...` (Task 1), WebSocket tick messages now carrying `kline_15m` (Task 1).
- Produces: `switchInterval(interval)` global function wired to the two new tab elements; `currentInterval` module-level variable other chart code can read.

There is no automated test for this task (no frontend test runner in this project — verification is manual, per the existing convention for UI-only changes in this codebase).

- [ ] **Step 1: Add the toggle CSS**

In `frontend/css/style.css`, after the existing `.order-tab:hover { color: var(--text); }` rule (line 316), add:
```css
.interval-tabs { display: flex; gap: 8px; align-items: center; }
.interval-tab {
  cursor: pointer; padding: 2px 8px; border-radius: 4px; font-size: 11px; color: var(--muted);
  transition: all .2s; user-select: none;
}
.interval-tab.active { background: var(--bg3); color: var(--text); font-weight: 600; }
.interval-tab:hover { color: var(--text); }
```
(Mirrors the existing `.order-tab` pattern exactly — same variables, same states — so the new toggle looks native to the rest of the page.)

- [ ] **Step 2: Add the toggle buttons and bump the cache-busting version**

In `frontend/index.html`, change:
```html
  <div class="main-grid">
    <div class="chart-panel">
      <div id="chart"></div>
      <div id="chart-legend" class="chart-legend"></div>
    </div>
```
to:
```html
  <div class="main-grid">
    <div class="chart-panel">
      <div class="interval-tabs">
        <span id="tab-interval-60" class="interval-tab active" onclick="switchInterval(60)">60分</span>
        <span id="tab-interval-15" class="interval-tab" onclick="switchInterval(15)">15分</span>
      </div>
      <div id="chart"></div>
      <div id="chart-legend" class="chart-legend"></div>
    </div>
```

Also in `frontend/index.html`, change line 146:
```html
  <script src="js/app.js?v=67"></script>
```
to:
```html
  <script src="js/app.js?v=68"></script>
```
(Cache-busting — without this, browsers/local caches may keep serving the pre-toggle `app.js`.)

- [ ] **Step 3: Add `currentInterval` state and `switchInterval()`**

In `frontend/js/app.js`, near the existing `let klinesCache = [];` (line 41), add:
```js
let currentInterval = 60;
```

Add a new function (near `switchOrderTab`, line 464, for proximity to the analogous existing pattern):
```js
function switchInterval(interval) {
  currentInterval = interval;
  const tab60 = document.getElementById('tab-interval-60');
  const tab15 = document.getElementById('tab-interval-15');
  if (tab60) tab60.classList.toggle('active', interval === 60);
  if (tab15) tab15.classList.toggle('active', interval === 15);
  chartInitialFitted = false;
  loadKlines();
}
```
(`chartInitialFitted = false` makes `loadKlines()` re-fit the visible range for the newly-loaded timeframe instead of keeping whatever range was scrolled to before the switch — see `loadKlines()`'s existing `if (!chartInitialFitted && ...)` block.)

- [ ] **Step 4: Thread `currentInterval` into the two fetch calls**

In `frontend/js/app.js`, change:
```js
async function loadKlines() {
  const res = await apiFetch(`${API}/api/klines?product=${currentProduct}&limit=500`);
```
to:
```js
async function loadKlines() {
  const res = await apiFetch(`${API}/api/klines?product=${currentProduct}&limit=500&interval=${currentInterval}`);
```

Change:
```js
    const [sigRes, tradeRes] = await Promise.all([
      apiFetch(`${API}/api/klines/signals?product=${currentProduct}&limit=500`),
      apiFetch(`${API}/api/strategy/trades`),
    ]);
```
to:
```js
    const [sigRes, tradeRes] = await Promise.all([
      apiFetch(`${API}/api/klines/signals?product=${currentProduct}&limit=500&interval=${currentInterval}`),
      apiFetch(`${API}/api/strategy/trades`),
    ]);
```

- [ ] **Step 5: Make the live WebSocket tick handler interval-aware**

In `frontend/js/app.js`, change:
```js
    if (msg.kline) {
      try {
        candleSeries.update({ ...msg.kline });
        updateLiveMA(msg.kline);
      } catch (_) { /* ignore duplicate time */ }
    }
```
to:
```js
    const liveKline = currentInterval === 15 ? msg.kline_15m : msg.kline;
    if (liveKline) {
      try {
        candleSeries.update({ ...liveKline });
        updateLiveMA(liveKline);
      } catch (_) { /* ignore duplicate time */ }
    }
```
(This is the fix a design review flagged: `updateLiveMA` must receive the same bar as `candleSeries.update`, or the MA5/MA20 lines would keep tracking 60-minute data even while viewing the 15-minute chart.)

- [ ] **Step 6: Manual verification**

Start the backend locally (or use the already-running dev instance) and open the dashboard in a browser:
1. Confirm the chart loads exactly as before (60分 tab active by default, 60-minute candles, breakout/pullback signal fields present via legend/crosshair — no visual change from before this task).
2. Click "15分" — confirm the chart re-draws with visibly narrower/more numerous candles, MA5/MA20/MA60 lines redraw, and no console errors.
3. With a live connection, watch a few ticks arrive on both the 60分 and 15分 views — confirm the current bar's candle body and the MA lines both visibly update (not just the candle).
4. Click back to "60分" — confirm it returns to the original view.

- [ ] **Step 7: Commit**

```bash
git add frontend/index.html frontend/css/style.css frontend/js/app.js
git commit -m "$(cat <<'EOF'
feat(dashboard): add 15-minute/60-minute chart toggle

New interval-tabs control (mirrors the existing order-tab pattern)
switches the chart between 60-minute (default, unchanged) and 15-minute
bars, threading interval=currentInterval through both kline-fetching
calls. The live WebSocket tick handler now picks kline vs kline_15m
based on the active tab, and updateLiveMA receives the same bar as the
candle series update (a design-review finding — previously MA lines
would have kept tracking 60-minute data even while viewing 15-minute
candles). Bumps the app.js cache-busting query string.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017ykxiou4TVNu3Q1mXgBLVg
EOF
)"
```

---

## Explicitly out of scope (per spec)

- Timeframes other than 15/60 minutes.
- Chart support for TX/MTX (still TMF-only, same reason as the rest of this feature line).
