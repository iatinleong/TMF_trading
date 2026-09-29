# 死叉做空策略 Phase 2：接上即時交易 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the already-backtested death-cross-short strategy (Phase 1, `commit d6b6fae`, SL/TP=130) into live trading as a 5th strategy alongside the existing 4, sharing all existing live infrastructure (OCO, soft-stop, risk insurance, reverse-signal-exit, reconnect reconciliation).

**Architecture:** Add a parallel, isolated 15-minute live bar-aggregation path (generalizing `kline_engine.py`'s existing 60-minute-only code, mirroring the `resample_to_nmin` pattern already used in Phase 1), hook it into the single real tick-ingestion point (`capital_futures.py::_on_tick_for_kline`) behind a try/except so it can never break the existing 60-minute path, then wire a 5th `STRATEGY_DEFS` entry into `strategy_service.py`'s existing generic per-strategy machinery. Finish with the mandatory Supabase binding (required for the Dashboard to even show the new strategy) and a fresh installer build.

**Tech Stack:** Python, pandas, pytest (existing project stack — no new dependencies).

Spec: `docs/superpowers/specs/2026-09-29-death-cross-live-trading-phase2-design.md`.

## Global Constraints

- The existing 60-minute live path (`get_store`, `LiveKlineStore` default behavior, `bar_close_time_from_ts` with no `minutes` arg) must not change behavior for any existing caller — every new parameter added in this plan has a default that reproduces current behavior exactly.
- The new 15-minute tick-ingestion call in `_on_tick_for_kline` must be wrapped in `try/except Exception` — a failure there must never propagate and must never prevent the existing 60-minute `on_tick()` call from running.
- Death-cross default risk parameters: `stop_loss_points=130.0`, `take_profit_points=130.0`, `reverse_signal_exit_enabled=False`. These must not change the defaults returned for `breakout_long`/`breakout_short`/`pullback_long`/`pullback_short`.
- Death-cross's K-bar sufficiency threshold is `DEFAULT_STRATEGY.ma_mid + 2` (22 bars), not `DEFAULT_STRATEGY.ma_slow + 2` (62 bars) — other strategies keep the 62-bar threshold unchanged.
- `_load_bars()` must skip the 60-minute CSV-merge fallback entirely when `interval_minutes != 60` — insufficient live 15-minute bars must never be padded with 60-minute CSV history.
- Product scope: TMF only (matches Phase 1 and the other 4 live strategies) — no TX/MTX live wiring in this plan.
- Ship as capability only: the new strategy must not auto-arm. Supabase binding happens without `--enabled`, and `--no-reverse-signal-exit-enabled` must be passed explicitly (see Task 4 — leaving it unset sends `true` from the frontend, not the intended `false`).
- No changes to `backend/broker/capital_futures.py::ingest_kline_row` or any other 60-minute-specific code path beyond what's listed in this plan.

---

## Task 1: Parameterize `kline_engine.py` for a 15-minute live store

**Files:**
- Modify: `backend/kline_engine.py` (whole file is ~192 lines; `bar_close_time_from_ts`, `LiveKlineStore`, `_cache_path`, `get_store`, plus a new `get_kline_store`)
- Test: `tests/test_kline_engine.py` (new file)

**Interfaces:**
- Produces: `bar_close_time_from_ts(ts: pd.Timestamp, minutes: int = 60) -> pd.Timestamp | None`; `LiveKlineStore(product_code: str, interval_minutes: int = 60)` with a new `self.interval_minutes` attribute; `get_kline_store(product_code: str, interval_minutes: int = 60) -> LiveKlineStore`; `get_store(product_code: str) -> LiveKlineStore` unchanged signature, now a thin wrapper.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_kline_engine.py
import pandas as pd

from backend.kline_engine import LiveKlineStore, bar_close_time_from_ts, get_kline_store, get_store


def test_bar_close_time_from_ts_default_60min_matches_hourly_bucket():
    ts = pd.Timestamp("2024-01-02 09:30:00")
    assert bar_close_time_from_ts(ts) == pd.Timestamp("2024-01-02 09:45:00")


def test_bar_close_time_from_ts_15min_buckets_correctly():
    ts = pd.Timestamp("2024-01-02 09:05:00")
    assert bar_close_time_from_ts(ts, minutes=15) == pd.Timestamp("2024-01-02 09:15:00")
    # boundary tick exactly on a bucket edge stays in that bucket, not the next one
    ts_edge = pd.Timestamp("2024-01-02 09:15:00")
    assert bar_close_time_from_ts(ts_edge, minutes=15) == pd.Timestamp("2024-01-02 09:15:00")


def test_live_kline_store_default_interval_is_60_minutes(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.kline_engine._CACHE_DIR", tmp_path)
    store = LiveKlineStore("TM_TEST_60")
    store.on_tick(100.0, 1, pd.Timestamp("2024-01-02 09:00:00"))
    store.on_tick(101.0, 1, pd.Timestamp("2024-01-02 09:50:00"))
    bars = store.get_klines()
    assert len(bars) == 1
    assert bars[0]["close"] == 101.0


def test_live_kline_store_15min_interval_creates_separate_bars(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.kline_engine._CACHE_DIR", tmp_path)
    store = LiveKlineStore("TM_TEST_15", interval_minutes=15)
    store.on_tick(100.0, 1, pd.Timestamp("2024-01-02 09:00:00"))
    store.on_tick(101.0, 1, pd.Timestamp("2024-01-02 09:20:00"))
    bars = store.get_klines()
    assert len(bars) == 2


def test_get_store_is_a_thin_wrapper_over_get_kline_store_60min(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.kline_engine._CACHE_DIR", tmp_path)
    a = get_store("TM_TEST_WRAP")
    b = get_kline_store("TM_TEST_WRAP", interval_minutes=60)
    assert a is b


def test_get_kline_store_15min_is_a_separate_store_from_60min(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.kline_engine._CACHE_DIR", tmp_path)
    a = get_store("TM_TEST_SEP")
    b = get_kline_store("TM_TEST_SEP", interval_minutes=15)
    assert a is not b
    assert b.interval_minutes == 15
    assert a.interval_minutes == 60
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_kline_engine.py -v`
Expected: FAIL — `bar_close_time_from_ts()` doesn't accept a `minutes` kwarg yet, `LiveKlineStore()` doesn't accept `interval_minutes`, `get_kline_store` doesn't exist.

- [ ] **Step 3: Implement the parameterization**

In `backend/kline_engine.py`, replace `_cache_path` through `get_store` (currently lines 33-192) with:

```python
def _cache_path(product_code: str, interval_minutes: int = 60) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", product_code.upper())
    suffix = "" if interval_minutes == 60 else f"_{interval_minutes}min"
    return _CACHE_DIR / f"{safe}{suffix}.json"


def bar_close_time_from_ts(ts: pd.Timestamp, minutes: int = 60) -> pd.Timestamp | None:
    """台指期交易時段內，將時間對齊到 `minutes` 分鐘寬的 K 棒收盤時間（預設60分鐘）。
    過濾週末與非交易時段：
    1. 日盤 (08:45 ~ 13:45): 週一(0) ~ 週五(4)
    2. 夜盤 (15:00 ~ 05:00): 週一(0) 15:00 到週六(5) 05:00 結束
    週六 05:00 以後、週日全天、週一 08:45 以前為休市時段。
    """
    ts = pd.Timestamp(ts)
    if ts.tzinfo is not None:
        ts = ts.tz_localize(None)

    weekday = ts.dayofweek  # 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun
    t = ts.time()

    is_day = (0 <= weekday <= 4) and (time(8, 45) <= t <= time(13, 45))
    is_night_evening = (0 <= weekday <= 4) and (t >= time(15, 0))  # 週一至週五 15:00 ~ 23:59
    is_night_morning = (1 <= weekday <= 5) and (t <= time(5, 0))   # 週二至週六 00:00 ~ 05:00 (屬於前一交易日夜盤)

    if not (is_day or is_night_evening or is_night_morning):
        return None

    trade_date = ts.normalize()
    if is_night_morning:
        trade_date = trade_date - pd.Timedelta(days=1)

    if is_day:
        session_start = trade_date + pd.Timedelta(hours=8, minutes=45)
    else:
        session_start = trade_date + pd.Timedelta(hours=15)

    elapsed = (ts - session_start) / pd.Timedelta(minutes=1)
    bar_no = int(np.ceil(max(float(elapsed), 0.0) / float(minutes)))
    if bar_no == 0:
        bar_no = 1
    return pd.Timestamp(session_start + pd.Timedelta(minutes=bar_no * minutes))


class LiveKlineStore:
    def __init__(self, product_code: str, interval_minutes: int = 60) -> None:
        self.product_code = product_code
        self.interval_minutes = interval_minutes
        self._bars: dict[int, dict] = {}
        self._load_cache()

    def _load_cache(self) -> None:
        path = _cache_path(self.product_code, self.interval_minutes)
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            self._bars = {int(key): bar for key, bar in raw.items()}
        except Exception:  # noqa: BLE001 - 快取壞掉不該讓啟動失敗，當作沒有快取
            logger.warning("讀取 K 線快取失敗（%s），略過", path, exc_info=True)

    def _persist(self) -> None:
        try:
            path = _cache_path(self.product_code, self.interval_minutes)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({str(key): bar for key, bar in self._bars.items()}),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception:  # noqa: BLE001 - 落地失敗不該影響即時交易主流程
            logger.warning("寫入 K 線快取失敗（%s）", self.product_code, exc_info=True)

    def _trim_bars(self) -> None:
        if len(self._bars) <= _BAR_LIMIT:
            return
        for key in sorted(self._bars.keys())[: len(self._bars) - _BAR_LIMIT]:
            del self._bars[key]

    def ingest_kline_row(self, raw: str) -> None:
        parsed = parse_kline_row(raw)
        if not parsed:
            return
        self._bars[int(parsed["time"])] = parsed
        self._trim_bars()

    def on_tick(self, price: float, volume: int, ts: pd.Timestamp | None = None) -> None:
        if price <= 0:
            return
        if ts is None or pd.isna(ts):
            ts = pd.Timestamp.now()
        close_time = bar_close_time_from_ts(ts, minutes=self.interval_minutes)
        if close_time is None:
            return
        # 2026-08-24 實測抓到的 bug：close_time 是 naive 台灣本地時間，pandas
        # 的 .timestamp() 對 naive 值一律當 UTC 處理，導致即時K棒的時間標籤
        # 比真實時間晚 8 小時（使用者看到「最新K棒」標成隔天，誤以為缺K棒）。
        key = to_unix_seconds(close_time)
        bar = self._bars.get(key)
        if bar is None:
            # 2026-08-25：開出新的一根，代表前一根（如果有）已經確定收盤，不會再
            # 有更新——這是唯一能確定某根 K 棒「定案」的時機點。這裡順手落地存檔，
            # 這樣後端重開後還讀得回這根，不用等群益 RequestKLineAMByDate 那個
            # 「整個交易時段收完盤才補齊」的回補（見檔頭常數註解）。
            if self._bars:
                self._persist()
            self._bars[key] = {
                "time": key,
                "open": float(price),
                "high": float(price),
                "low": float(price),
                "close": float(price),
                "volume": max(int(volume), 0),
            }
        else:
            bar["high"] = max(float(bar["high"]), float(price))
            bar["low"] = min(float(bar["low"]), float(price))
            bar["close"] = float(price)
            if volume > 0:
                bar["volume"] = int(bar.get("volume", 0)) + int(volume)
        self._trim_bars()

    def get_klines(self, limit: int = DEFAULT_KLINE_LIMIT) -> list[dict]:
        ordered = sorted(self._bars.values(), key=lambda b: int(b["time"]))
        if limit > 0:
            ordered = ordered[-limit:]
        return [dict(b) for b in ordered]

    def latest_bar(self) -> dict | None:
        klines = self.get_klines(limit=1)
        return klines[-1] if klines else None

    def merge_missing_bars(self, bars: list[dict]) -> int:
        """
        2026-08-25：補外部備援資料源（例如 Shioaji，見 backend/shioaji_backfill.py）
        用——群益 RequestKLineAMByDate 對「進行中交易時段」已經收盤的 K 棒不給
        資料（見檔頭常數註解），這裡讓別的資料源補這個空窗期。只寫入這裡還沒有
        資料的時間點，不覆蓋既有資料（群益回補/即時 tick 已經有的優先，備援
        資料源只補真正缺的）。整批處理完才落地存檔一次，避免補很多根時重複
        寫檔。回傳實際新增的根數。
        """
        added = 0
        for bar in bars:
            key = int(bar["time"])
            if key in self._bars:
                continue
            self._bars[key] = dict(bar)
            added += 1
        if added:
            self._trim_bars()
            self._persist()
        return added


_stores: dict[tuple[str, int], LiveKlineStore] = {}


def get_kline_store(product_code: str, interval_minutes: int = 60) -> LiveKlineStore:
    code = product_code.upper()
    key = (code, interval_minutes)
    if key not in _stores:
        _stores[key] = LiveKlineStore(code, interval_minutes=interval_minutes)
    return _stores[key]


def get_store(product_code: str) -> LiveKlineStore:
    return get_kline_store(product_code, interval_minutes=60)
```

Note what changed from the original: `_cache_path`/`LiveKlineStore.__init__`/`_load_cache`/`_persist` gained `interval_minutes`; `on_tick` passes `minutes=self.interval_minutes` to `bar_close_time_from_ts`; `_stores` is now keyed by `(code, interval_minutes)` tuples instead of bare `code` strings; `get_store` is now a one-line wrapper. `ingest_kline_row`, `get_klines`, `latest_bar`, `merge_missing_bars` are unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_kline_engine.py -v`
Expected: 7 passed.

- [ ] **Step 5: Run the full test suite to confirm no regressions**

Run: `python -m pytest tests/ -q`
Expected: all tests pass (existing count + 7 new).

- [ ] **Step 6: Commit**

```bash
git add backend/kline_engine.py tests/test_kline_engine.py
git commit -m "$(cat <<'EOF'
feat(kline-engine): parameterize LiveKlineStore for a 15-minute live store

Generalizes bar_close_time_from_ts and LiveKlineStore with an
interval_minutes parameter (default 60, matching current behavior
exactly), and adds get_kline_store(product_code, interval_minutes) as
the new generic entry point. get_store() becomes a thin wrapper over
get_kline_store(..., interval_minutes=60) — every existing caller
(api.py, live_service.py, shioaji_backfill.py, strategy_service.py)
needs zero changes. Separate cache files per interval avoid the two
timelines' persisted bars colliding.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017ykxiou4TVNu3Q1mXgBLVg
EOF
)"
```

---

## Task 2: Hook the 15-minute store into live tick ingestion

**Files:**
- Modify: `backend/broker/capital_futures.py:1686-1703` (`_on_tick_for_kline`, plus its two local `try`/`except ImportError` import blocks)
- Test: `tests/test_capital_futures_broker.py`

**Interfaces:**
- Consumes: `get_kline_store` (Task 1).
- Produces: every live tick now also feeds a 15-minute `LiveKlineStore` for the current subscribed product, isolated from the existing 60-minute path by a try/except.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_capital_futures_broker.py`. First extend the existing import line `from backend.kline_engine import get_store` to also import `get_kline_store`:

```python
from backend.kline_engine import get_kline_store, get_store
```

Then add these two tests (place them near `test_on_tick_for_kline_polling_path_uses_taipei_tz_not_system_clock`, reusing the same fixed-clock pattern so the test doesn't depend on real wall-clock time falling inside a trading session):

```python
def _patched_now_in_day_session():
    """Returns a datetime subclass whose .now() is pinned to a fixed instant that
    maps to Taiwan time 2026-08-25 10:00:00 (inside the day session), so tests
    don't depend on when they actually run."""
    fixed_utc = datetime(2026, 8, 25, 2, 0, 0, tzinfo=timezone.utc)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_utc.replace(tzinfo=None)
            return fixed_utc.astimezone(tz)

    return _FixedDatetime


def test_on_tick_for_kline_feeds_both_60min_and_15min_stores():
    broker = CapitalFuturesBroker()
    broker.live.subscribed_product = "TM_TEST_DUAL"

    with patch("backend.broker.capital_futures.datetime", _patched_now_in_day_session()):
        broker._on_tick_for_kline(21500.0, 1, 0, 0)

    bar_60 = get_store("TM_TEST_DUAL").latest_bar()
    bar_15 = get_kline_store("TM_TEST_DUAL", interval_minutes=15).latest_bar()
    assert bar_60 is not None
    assert bar_15 is not None


def test_on_tick_for_kline_60min_path_survives_15min_store_failure():
    broker = CapitalFuturesBroker()
    broker.live.subscribed_product = "TM_TEST_FAIL15"

    with patch("backend.broker.capital_futures.datetime", _patched_now_in_day_session()), \
         patch("backend.kline_engine.get_kline_store", side_effect=RuntimeError("boom")):
        broker._on_tick_for_kline(21500.0, 1, 0, 0)  # must not raise

    bar_60 = get_store("TM_TEST_FAIL15").latest_bar()
    assert bar_60 is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_capital_futures_broker.py -k "dual or survives" -v`
Expected: `test_on_tick_for_kline_feeds_both_60min_and_15min_stores` FAILs (`bar_15` is `None`, since nothing feeds the 15-minute store yet). `test_on_tick_for_kline_60min_path_survives_15min_store_failure` currently PASSes trivially (there's no 15-minute call yet to fail) — that's expected at this point; it becomes a meaningful regression guard after Step 3.

- [ ] **Step 3: Implement the hook**

In `backend/broker/capital_futures.py`, find `_on_tick_for_kline` (currently lines 1678-1703):

```python
    def _on_tick_for_kline(
        self, price: float, volume: int, n_date: int, n_timehms: int
    ) -> None:
        if price <= 0:
            return
        product = self.live.quote.product_code or self.live.subscribed_product
        if not product:
            return
        try:
            from ..kline_engine import get_store
            from ..timeutil import TAIPEI_TZ
        except ImportError:
            from kline_engine import get_store  # type: ignore
            from timeutil import TAIPEI_TZ  # type: ignore

        ts = (
            _capital_tick_timestamp(n_date, n_timehms)
            if n_date > 0
            else pd.Timestamp(datetime.now(TAIPEI_TZ))
        )
        get_store(product).on_tick(price, volume, ts)
```

Replace the two import lines and the final line:

```python
    def _on_tick_for_kline(
        self, price: float, volume: int, n_date: int, n_timehms: int
    ) -> None:
        if price <= 0:
            return
        product = self.live.quote.product_code or self.live.subscribed_product
        if not product:
            return
        try:
            from ..kline_engine import get_kline_store, get_store
            from ..timeutil import TAIPEI_TZ
        except ImportError:
            from kline_engine import get_kline_store, get_store  # type: ignore
            from timeutil import TAIPEI_TZ  # type: ignore

        ts = (
            _capital_tick_timestamp(n_date, n_timehms)
            if n_date > 0
            else pd.Timestamp(datetime.now(TAIPEI_TZ))
        )
        get_store(product).on_tick(price, volume, ts)
        try:
            get_kline_store(product, interval_minutes=15).on_tick(price, volume, ts)
        except Exception:  # noqa: BLE001 - 15分鐘聚合失敗絕不能影響既有60分鐘路徑或COM事件執行緒
            logger.warning("15分鐘K棒聚合失敗（不影響既有60分鐘路徑）", exc_info=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_capital_futures_broker.py -k "dual or survives or on_tick_for_kline" -v`
Expected: 3 passed (the 2 new tests plus the pre-existing `test_on_tick_for_kline_polling_path_uses_taipei_tz_not_system_clock`).

- [ ] **Step 5: Run the full test suite**

Run: `python -m pytest tests/ -q`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add backend/broker/capital_futures.py tests/test_capital_futures_broker.py
git commit -m "$(cat <<'EOF'
feat(capital-futures): feed a parallel 15-minute kline store on every tick

_on_tick_for_kline now also calls get_kline_store(product,
interval_minutes=15).on_tick(...) right after the existing 60-minute
get_store(product).on_tick(...) call. Wrapped in try/except so a failure
in the new 15-minute path can never break the existing, incident-prone
60-minute live path or propagate out of a COM event callback.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017ykxiou4TVNu3Q1mXgBLVg
EOF
)"
```

---

## Task 3: Wire `death_cross_short` into `strategy_service.py`

**Files:**
- Modify: `backend/strategy_service.py` (imports at line 27-28, `STRATEGY_DEFS` at line 45, `_bars_from_kline_store`/`_load_bars` at lines 524-595, `_signaled_frame` at lines 615-618, `strategy_config_defaults` at lines 691-707, `start_strategy` at lines 798-801, the rollover block inside `_tick_one` at lines ~991-1005, the bar-sufficiency gate inside `_tick_one` at lines ~1142-1147)
- Test: `tests/test_strategy_service.py`

**Interfaces:**
- Consumes: `get_kline_store` (Task 1), `generate_death_cross_signals` (already in `backend/signals.py` from Phase 1).
- Produces: `STRATEGY_DEFS["death_cross_short"]`; `strategy_config_defaults("death_cross_short")` returning `stop_loss_points=130.0, take_profit_points=130.0, reverse_signal_exit_enabled=False`; `_load_bars(contract, product_code=None, *, interval_minutes=60, min_bars_required=None)` (new keyword-only params, both optional with defaults matching current behavior).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_strategy_service.py`. Extend the existing `from backend.strategy_service import (...)` block to add `_load_bars`:

```python
from backend.strategy_service import (
    StrategyState,
    _armed,
    _canonical_position_product,
    _cancel_protection_order_for_state,
    _check_orphaned_fill,
    _load_bars,
    _opposite_direction_position_exists,
    _place_oco_protection_for_state,
    _settlement_month_from_tmf_code,
    _tick_one,
    reconcile_after_manual_close,
    reconcile_orphan_stop_orders,
    strategy_config_defaults,
    stop_strategy,
)
```

```python
def test_strategy_config_defaults_death_cross_uses_130_points_and_no_reverse_exit():
    defaults = strategy_config_defaults("death_cross_short")
    assert defaults["stop_loss_points"] == pytest.approx(130.0)
    assert defaults["take_profit_points"] == pytest.approx(130.0)
    assert defaults["reverse_signal_exit_enabled"] is False
    assert defaults["strategy"] == "death_cross"
    assert defaults["direction_limit"] == "short"


def test_strategy_config_defaults_breakout_unaffected_by_death_cross_branch():
    defaults = strategy_config_defaults("breakout_long")
    assert defaults["stop_loss_points"] == pytest.approx(100.0)
    assert defaults["take_profit_points"] == pytest.approx(250.0)
    assert defaults["reverse_signal_exit_enabled"] is True


def test_load_bars_death_cross_uses_15min_store_and_skips_csv_merge(monkeypatch):
    """death_cross 傳 interval_minutes=15 時：(1) 讀的是 15 分鐘 store 不是 60 分鐘；
    (2) 即時K棒不足時直接回傳現況，不觸發 60 分鐘 CSV 合併分支。"""
    calls = []

    def _fake_get_kline_store(product_code, interval_minutes=60):
        calls.append(interval_minutes)
        store = MagicMock()
        store.get_klines.return_value = []  # 模擬即時資料完全不足
        return store

    monkeypatch.setattr(strategy_service, "get_kline_store", _fake_get_kline_store)
    # 60 分鐘 CSV 暖機檔存在與否都不該被讀到 —— 用一個一定會失敗的路徑戳穿它，
    # 如果程式碼誤觸發 CSV 合併分支，這裡就會因為檔案不存在而回傳空的 csv_bars，
    # 但我們真正要驗證的是「根本不會走到那段程式碼」，所以改成監看是否呼叫。
    bars = _load_bars("TMF", product_code="TM2609", interval_minutes=15, min_bars_required=22)

    assert calls == [15]
    assert bars.empty


def test_load_bars_default_60min_behavior_unchanged(monkeypatch):
    """不傳 interval_minutes/min_bars_required 時，行為必須跟修改前完全一樣：讀 60 分鐘 store。"""
    calls = []

    def _fake_get_kline_store(product_code, interval_minutes=60):
        calls.append(interval_minutes)
        store = MagicMock()
        store.get_klines.return_value = []
        return store

    monkeypatch.setattr(strategy_service, "get_kline_store", _fake_get_kline_store)
    _load_bars("TMF", product_code="TM2609")

    assert calls == [60]


def _death_cross_short_state(**overrides) -> StrategyState:
    defaults = dict(
        strategy_id="death_cross_short",
        product_code="TM2609",
        strategy="death_cross",
        direction_limit="short",
        label="死叉做空",
        qty=1,
        initial_capital_ntd=100_000,
        max_loss_ntd=10_000,
        max_loss_pct=0.99,
        stop_loss_points=130.0,
        take_profit_points=130.0,
        reverse_signal_exit_enabled=False,
    )
    defaults.update(overrides)
    return StrategyState(**defaults)


def test_tick_one_death_cross_enters_short_via_signal(clean_armed):
    """跟現有 test_tick_one_entry_places_oco_protection 同款手法：mock
    _latest_closed_signal/_load_bars/_signaled_frame，驗證死叉策略走同一套
    generic 進場+OCO 機制，不需要真的產生死叉訊號的 K 棒資料。"""
    state = _death_cross_short_state(held_qty=0, held_direction=None, entry_price=0.0)
    state.last_signal_key = None
    svc = MagicMock()
    svc.place_order.return_value = {"order_result": {"success": True}}
    svc.place_oco_order.return_value = _oco_success_response()
    st = {"quote": {"last_price": 21400.0}}

    fake_bars = MagicMock()
    fake_bars.empty = False
    fake_bars.__len__.return_value = 100

    with patch.object(
        strategy_service,
        "_latest_closed_signal",
        return_value={"time": "2026-08-25T09:00:00", "direction": "short", "key": "dc-1"},
    ), patch.object(strategy_service, "_load_bars", return_value=fake_bars), patch.object(
        strategy_service, "_signaled_frame", return_value=MagicMock()
    ):
        _tick_one(state, svc, st)

    assert state.held_qty == 1
    assert state.held_direction == "short"
    assert state.entry_price == pytest.approx(21400.0)
    svc.place_oco_order.assert_called_once()
    assert state.protection_order_smart_key == "26564233"


def test_tick_one_death_cross_ignores_golden_cross_when_reverse_exit_disabled(clean_armed):
    """跟現有 test_tick_one_reverse_signal_skipped_when_disabled 同款手法：已持有
    死叉空單，遇到金叉（"long"）訊號，reverse_signal_exit_enabled=False 時應該
    完全不動作，只靠 130 點 SL/TP 出場。"""
    state = _death_cross_short_state(
        held_qty=1, held_direction="short", entry_price=21500.0,
    )
    state.last_signal_key = "previous-key"
    svc = MagicMock()
    st = {"quote": {"last_price": 21500.0}}  # 沒有觸及130點SL/TP，不會被那條路徑攔截

    fake_bars = MagicMock()
    fake_bars.empty = False
    fake_bars.__len__.return_value = 100

    with patch.object(
        strategy_service,
        "_latest_closed_signal",
        return_value={"time": "2026-08-25T09:15:00", "direction": "long", "key": "dc-golden-1"},
    ), patch.object(strategy_service, "_load_bars", return_value=fake_bars), patch.object(
        strategy_service, "_signaled_frame", return_value=MagicMock()
    ):
        _tick_one(state, svc, st)

    svc.place_order.assert_not_called()
    assert state.held_qty == 1
    assert state.held_direction == "short"
    assert "已關閉反向訊號出場" in state.last_action
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_strategy_service.py -k "death_cross or load_bars_default" -v`
Expected: FAIL — `strategy_config_defaults("death_cross_short")` returns empty/wrong values (`death_cross_short` isn't in `STRATEGY_DEFS` yet), `_load_bars()` doesn't accept `interval_minutes`/`min_bars_required` kwargs yet, `strategy_service.get_kline_store` doesn't exist as an attribute yet.

- [ ] **Step 3: Implement the wiring**

3a. Import — in `backend/strategy_service.py`, change line 27-28:
```python
from .config import DEFAULT_STRATEGY, ContractSpec, app_base_dir
from .kline_engine import get_store
```
to:
```python
from .config import DEFAULT_STRATEGY, ContractSpec, app_base_dir
from .kline_engine import get_kline_store, get_store
```

Also change the `.signals` import line (find `from .signals import generate_breakout_signals, generate_pullback_signals`) to:
```python
from .signals import generate_breakout_signals, generate_death_cross_signals, generate_pullback_signals
```

3b. `STRATEGY_DEFS` — add the 5th entry:
```python
STRATEGY_DEFS: dict[str, dict[str, str]] = {
    "breakout_long": {"strategy": "breakout", "direction_limit": "long", "label": "突破做多"},
    "breakout_short": {"strategy": "breakout", "direction_limit": "short", "label": "突破做空"},
    "pullback_long": {"strategy": "pullback", "direction_limit": "long", "label": "回測做多"},
    "pullback_short": {"strategy": "pullback", "direction_limit": "short", "label": "回測做空"},
    "death_cross_short": {"strategy": "death_cross", "direction_limit": "short", "label": "死叉做空"},
}
```

3c. Add two small helpers right after `STRATEGY_DEFS`:
```python
def _interval_minutes_for_strategy(strategy: str) -> int:
    return 15 if strategy == "death_cross" else 60


def _min_bars_required_for_strategy(strategy: str) -> int:
    return DEFAULT_STRATEGY.ma_mid + 2 if strategy == "death_cross" else DEFAULT_STRATEGY.ma_slow + 2
```

3d. `_bars_from_kline_store`/`_load_bars` — change:
```python
def _bars_from_kline_store(product_code: str) -> pd.DataFrame:
    klines = get_store(product_code).get_klines(limit=500)
```
to:
```python
def _bars_from_kline_store(product_code: str, interval_minutes: int = 60) -> pd.DataFrame:
    klines = get_kline_store(product_code, interval_minutes=interval_minutes).get_klines(limit=500)
```
(the rest of `_bars_from_kline_store`'s body is unchanged).

Change:
```python
def _load_bars(contract: str, product_code: str | None = None) -> pd.DataFrame:
    live_bars = pd.DataFrame()
    if product_code:
        live_bars = _bars_from_kline_store(product_code)
        if not live_bars.empty and len(live_bars) >= DEFAULT_STRATEGY.ma_slow + 2:
            return live_bars

    csv_bars = pd.DataFrame()
```
to:
```python
def _load_bars(
    contract: str,
    product_code: str | None = None,
    *,
    interval_minutes: int = 60,
    min_bars_required: int | None = None,
) -> pd.DataFrame:
    if min_bars_required is None:
        min_bars_required = DEFAULT_STRATEGY.ma_slow + 2

    live_bars = pd.DataFrame()
    if product_code:
        live_bars = _bars_from_kline_store(product_code, interval_minutes=interval_minutes)
        if not live_bars.empty and len(live_bars) >= min_bars_required:
            return live_bars
        if interval_minutes != 60:
            # 沒有對應時間軸的 CSV 暖機檔可用；即時資料不足時直接回傳現況
            # （可能是空的），不能退回 60 分鐘 CSV 合併，否則會把不同時間軸的
            # K棒混在一起，弄亂 MA 計算。
            return live_bars

    csv_bars = pd.DataFrame()
```
(everything from `csv_bars = pd.DataFrame()` onward — the CSV-merge logic — is unchanged).

3e. `_signaled_frame` — change:
```python
def _signaled_frame(strategy: str, bars: pd.DataFrame) -> pd.DataFrame:
    if strategy == "breakout":
        return generate_breakout_signals(bars)
    return generate_pullback_signals(bars)
```
to:
```python
def _signaled_frame(strategy: str, bars: pd.DataFrame) -> pd.DataFrame:
    if strategy == "breakout":
        return generate_breakout_signals(bars)
    if strategy == "death_cross":
        return generate_death_cross_signals(bars)
    return generate_pullback_signals(bars)
```

3f. `strategy_config_defaults` — change:
```python
def strategy_config_defaults(strategy_id: str | None = None) -> dict[str, Any]:
    defs = STRATEGY_DEFS.get(strategy_id, {}) if strategy_id else {}
    return {
        "strategy": defs.get("strategy", "pullback"),
        "direction_limit": defs.get("direction_limit", "long"),
        "label": defs.get("label", strategy_id or ""),
        "qty": _env_int("STRATEGY_QTY", 1),
        "initial_capital_ntd": _fetch_equity_basis(),
        "max_loss_ntd": _env_float("STRATEGY_MAX_LOSS_NTD", 10_000.0),
        "max_loss_pct": _env_float("STRATEGY_MAX_LOSS_PCT", 0.10),
        "stop_loss_points": _env_float("STRATEGY_STOP_LOSS_POINTS", 100.0),
        "take_profit_points": _env_float("STRATEGY_TAKE_PROFIT_POINTS", 250.0),
        "oco_enabled": True,
        "soft_stop_enabled": True,
        "risk_insurance_enabled": True,
        "reverse_signal_exit_enabled": True,
    }
```
to:
```python
def strategy_config_defaults(strategy_id: str | None = None) -> dict[str, Any]:
    defs = STRATEGY_DEFS.get(strategy_id, {}) if strategy_id else {}
    strategy_kind = defs.get("strategy", "pullback")
    is_death_cross = strategy_kind == "death_cross"
    return {
        "strategy": strategy_kind,
        "direction_limit": defs.get("direction_limit", "long"),
        "label": defs.get("label", strategy_id or ""),
        "qty": _env_int("STRATEGY_QTY", 1),
        "initial_capital_ntd": _fetch_equity_basis(),
        "max_loss_ntd": _env_float("STRATEGY_MAX_LOSS_NTD", 10_000.0),
        "max_loss_pct": _env_float("STRATEGY_MAX_LOSS_PCT", 0.10),
        "stop_loss_points": 130.0 if is_death_cross else _env_float("STRATEGY_STOP_LOSS_POINTS", 100.0),
        "take_profit_points": 130.0 if is_death_cross else _env_float("STRATEGY_TAKE_PROFIT_POINTS", 250.0),
        "oco_enabled": True,
        "soft_stop_enabled": True,
        "risk_insurance_enabled": True,
        "reverse_signal_exit_enabled": not is_death_cross,
    }
```

3g. `start_strategy` — change (lines 798-801):
```python
    contract = _contract_from_product(effective_product_code)
    bars = _load_bars(contract, product_code=effective_product_code)
    if not bars.empty and len(bars) >= DEFAULT_STRATEGY.ma_slow + 2:
        existing_signal = _latest_closed_signal(_signaled_frame(state.strategy, bars))
        if existing_signal:
            state.last_signal_key = existing_signal["key"]
```
to:
```python
    contract = _contract_from_product(effective_product_code)
    bars = _load_bars(
        contract,
        product_code=effective_product_code,
        interval_minutes=_interval_minutes_for_strategy(state.strategy),
        min_bars_required=_min_bars_required_for_strategy(state.strategy),
    )
    if not bars.empty and len(bars) >= _min_bars_required_for_strategy(state.strategy):
        existing_signal = _latest_closed_signal(_signaled_frame(state.strategy, bars))
        if existing_signal:
            state.last_signal_key = existing_signal["key"]
```

3h. Rollover block inside `_tick_one` — change:
```python
            new_contract = _contract_from_product(current_tmf)
            new_bars = _load_bars(new_contract, product_code=current_tmf)
            if not new_bars.empty and len(new_bars) >= DEFAULT_STRATEGY.ma_slow + 2:
                existing_sig = _latest_closed_signal(_signaled_frame(state.strategy, new_bars))
                state.last_signal_key = existing_sig["key"] if existing_sig else None
            else:
                state.last_signal_key = None
```
to:
```python
            new_contract = _contract_from_product(current_tmf)
            new_bars = _load_bars(
                new_contract,
                product_code=current_tmf,
                interval_minutes=_interval_minutes_for_strategy(state.strategy),
                min_bars_required=_min_bars_required_for_strategy(state.strategy),
            )
            if not new_bars.empty and len(new_bars) >= _min_bars_required_for_strategy(state.strategy):
                existing_sig = _latest_closed_signal(_signaled_frame(state.strategy, new_bars))
                state.last_signal_key = existing_sig["key"] if existing_sig else None
            else:
                state.last_signal_key = None
```

3i. Bar-sufficiency gate inside `_tick_one` — change:
```python
    bars = _load_bars(contract, product_code=state.product_code)
    if bars.empty or len(bars) < DEFAULT_STRATEGY.ma_slow + 2:
        state.last_action = "K 棒資料不足，略過本輪"
        return

    signaled = _signaled_frame(state.strategy, bars)
```
to:
```python
    bars = _load_bars(
        contract,
        product_code=state.product_code,
        interval_minutes=_interval_minutes_for_strategy(state.strategy),
        min_bars_required=_min_bars_required_for_strategy(state.strategy),
    )
    if bars.empty or len(bars) < _min_bars_required_for_strategy(state.strategy):
        state.last_action = "K 棒資料不足，略過本輪"
        return

    signaled = _signaled_frame(state.strategy, bars)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_strategy_service.py -k "death_cross or load_bars_default" -v`
Expected: 6 passed.

- [ ] **Step 5: Run the full test suite**

Run: `python -m pytest tests/ -q`
Expected: all tests pass — this step matters more than usual here, since Task 3 touches 4 call sites shared with the 3 existing live strategies; any regression there would show up as an existing test failing.

- [ ] **Step 6: Commit**

```bash
git add backend/strategy_service.py tests/test_strategy_service.py
git commit -m "$(cat <<'EOF'
feat(strategy-service): wire death_cross_short into the live strategy engine

Adds STRATEGY_DEFS["death_cross_short"], and threads interval_minutes/
min_bars_required through _load_bars/_bars_from_kline_store/start_strategy/
_tick_one's rollover and bar-sufficiency-gate call sites via two small
helpers (_interval_minutes_for_strategy, _min_bars_required_for_strategy).
death_cross gets its own 130/130/reverse_signal_exit_enabled=False
defaults in strategy_config_defaults() without changing what the other
4 strategies resolve to (verified by a dedicated test). _load_bars() now
skips the 60-minute CSV-merge fallback entirely for any non-60-minute
interval, so insufficient 15-minute live bars are never padded with
60-minute CSV history.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017ykxiou4TVNu3Q1mXgBLVg
EOF
)"
```

---

## Task 4: Supabase binding, Dashboard cosmetic fix, and installer

**Files:**
- Modify: `frontend/js/app.js:256-259` (`STRATEGY_SHORT_CODE`)
- Run: `scripts/bind_strategy.py` (no code changes — this script already supports everything needed)
- Rebuild: `installer/Output/TMF-Trading-Setup.exe`

**Interfaces:**
- Consumes: `STRATEGY_DEFS["death_cross_short"]` (Task 3) — `bind_strategy.py --list-strategies` should show it.

- [ ] **Step 1: Add the frontend short-code entry**

In `frontend/js/app.js`, change:
```js
const STRATEGY_SHORT_CODE = {
  breakout_long: 'BL', breakout_short: 'BS',
  pullback_long: 'PL', pullback_short: 'PS',
};
```
to:
```js
const STRATEGY_SHORT_CODE = {
  breakout_long: 'BL', breakout_short: 'BS',
  pullback_long: 'PL', pullback_short: 'PS',
  death_cross_short: 'DS',
};
```

- [ ] **Step 2: Run the full backend test suite one more time before touching anything live**

Run: `python -m pytest tests/ -q`
Expected: all tests pass (this confirms Tasks 1-3 are still green together before Step 3 does something with real side effects).

- [ ] **Step 3: Confirm the strategy is listed, then bind it to the account (real Supabase write — confirm with the user before running)**

```bash
python scripts/bind_strategy.py --list-strategies
```
Expected output includes a `death_cross_short	死叉做空（death_cross / short）` line.

Then, after checking with the user that this is the correct account UUID (this is a real write to the production Supabase table):
```bash
python scripts/bind_strategy.py --user-id 01e79d6d-a686-4588-bdd2-5329512b97ef --strategy-id death_cross_short --no-reverse-signal-exit-enabled
```
Do **not** pass `--enabled` (ships as capability only, per the spec's rollout decision). Leave `--stop-loss-points`/`--take-profit-points`/`--qty`/`--product-code` unset — they fall through to `strategy_config_defaults()`'s 130/130/qty=1/auto-resolved-TMF-front-month defaults.

- [ ] **Step 4: Manually verify in the browser**

Log into the Dashboard (wherever it's currently reachable — local dev server or the VM) and confirm:
1. A 5th strategy card labeled "死叉做空" appears in the strategy list.
2. Its summary line shows 停損:130點 · 停利:130點.
3. Its "4.反向平倉" badge shows ✗ (off) — this is the check that actually proves the `--no-reverse-signal-exit-enabled` bind worked; if it shows ✓, the bind command needs to be re-run.
4. The on/off switch toggles without error (toggle it on then immediately off again — do not leave it armed unless you intend to start live trading with real money).

- [ ] **Step 5: Rebuild the installer**

```bash
python -m PyInstaller tmf-backend.spec --noconfirm
"/c/Users/user/AppData/Local/Programs/Inno Setup 6/ISCC.exe" installer/tmf_trading_setup.iss
```
Confirm `installer/Output/TMF-Trading-Setup.exe` was rebuilt (check its modified timestamp is now).

- [ ] **Step 6: Commit the frontend change**

```bash
git add frontend/js/app.js
git commit -m "$(cat <<'EOF'
feat(dashboard): add short code for the death-cross-short strategy

Cosmetic only — chart entry/exit arrow labels show "DS" instead of the
full "death_cross_short" string. STRATEGY_SHORT_CODE already has a
fallback (STRATEGY_SHORT_CODE[sid] || sid), so this was never required
for correctness, just readability.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_017ykxiou4TVNu3Q1mXgBLVg
EOF
)"
```

(The Supabase bind in Step 3 is a database write, not a file change — nothing to commit for it. The installer rebuild in Step 5 produces a local binary under `installer/Output/`, which is already gitignored.)

---

## Explicitly out of scope (per spec — not part of this plan)

- TX/MTX live trading (OCO smart-order handling is only verified for TMF's explicit month-code format).
- Day/night session entry filtering (mentioned in the original requirements but deferred past both Phase 1 and Phase 2).
- Further parameter exploration (MA period combinations, asymmetric SL/TP, stricter slope filtering) — the user chose to ship the original 130/130 spec as-is.
- Deploying to the actual production VM — this plan produces a rebuilt installer; RDP-ing in and running it is a separate, user-timed step.
