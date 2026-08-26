# 帳號 ↔ 策略配對（Plan A：資料層，不含多帳號並發下單）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **2026-08-26 事後更新：實際做出來的跟下面 Task 4/6 寫的不一樣，以這段為準。**
> 執行完 Task 1-6 之後，根據回饋修正了兩個地方：
> 1. **沒有「管理員：策略客製化」網頁表單**——「客製化」指的是幫客戶另外寫一份策略程式碼（不同進出場邏輯），不是網頁表單能表達的東西。綁定改成用 `scripts/bind_strategy.py`（CLI 工具）直接寫資料庫，`/api/admin/strategy-defs`、`/api/admin/strategy-configs`、`/api/my-strategy-configs/toggle`、`_is_admin`/`ADMIN_EMAILS` 都已經拿掉。
> 2. **沒有獨立的「我的策略」頁面**——綁定給帳號的策略，直接顯示在既有的「自動策略」面板裡（過濾掉沒綁定的），不是另一個分頁/導覽連結。`frontend/strategy-config.html`/`strategy-config.js` 已刪除。
>
> `user_strategy_configs` 表、`backend/strategy_config_store.py`、`/api/my-strategy-configs`（唯讀）這些底層資料層維持不變，只是存取方式（CLI 而非網頁）跟顯示位置（既有面板而非獨立頁）改了。

**Goal:** 每個登入帳號的「策略」欄位預設是空的；只有管理員（你自己）針對特定使用者的需求，手動把 `STRATEGY_DEFS` 裡的某個策略客製化綁定給那個帳號（設定商品代碼/口數），使用者自己只能對「已經綁定給他的」策略按開關（啟用/停用），不能自己新增或改參數。你目前這個帳號本身已經在跑 4 個策略（`breakout_long`/`breakout_short`/`pullback_long`/`pullback_short`），這個計畫會把這個現況原封不動地寫成第一批資料。

**Architecture:** 沿用專案既有的「輕量 REST 呼叫 Supabase，不用完整 SDK」慣例（見 `backend/secrets_store.py`），新增一張 `user_strategy_configs` 表。後端在 `require_supabase_auth` middleware 驗證 JWT 後，把 `claims["sub"]`/`claims["email"]` 掛到 `request.state`；一般使用者端點只能讀/切換自己 `user_id` 底下已存在的列，新增/改參數的寫入動作只開放給 `ADMIN_EMAILS` 環境變數允許清單裡的信箱。**這個計畫刻意不觸碰 `live_service.py`/`strategy_service.py` 的執行邏輯**——這兩個模組目前是全域單例（一份持倉、一條 SKCOM 連線），「一台機器同時並發多個不同帳號登入、各自執行策略」是否可行，仍待用兩個真實帳號實測（見對話紀錄）。這個計畫只把「哪個帳號綁定了哪個策略、什麼參數、有沒有開啟」這個設定層先做出來，之後並發登入確認可行了，才會有 Plan B 去改 `live_service.py`/`strategy_service.py` 讓執行真的按登入帳號分流。

**Tech Stack:** FastAPI（既有）、Supabase REST API（既有 `SUPABASE_URL`/`SUPABASE_SECRET_KEY`，見 `backend/secrets_store.py`）、pytest + `unittest.mock`（既有測試慣例）、原生 JS（無框架，跟現有 `frontend/js/*.js` 一致）。

## Global Constraints

- 不修改 `backend/live_service.py`、`backend/strategy_service.py` 的執行邏輯（`start_strategy`/`strategy_tick`/持倉追蹤）——這個計畫只做設定層。
- 新的 API 端點一律走既有的 `require_supabase_auth` middleware（`backend/api.py:128`），不另外設計驗證機制。
- **權限模型**：一般使用者只能讀自己的策略設定、切換自己已存在那幾列的 `enabled`；新增一列（把某個策略客製化綁定給某個帳號）或修改參數，只有 `ADMIN_EMAILS` 環境變數（逗號分隔的信箱清單）裡的信箱能做。這是刻意選的最簡單做法（一個 env var，不建角色資料表），因為現階段只有你一個人是管理員——之後真的需要多個管理員/更細的權限，再另外規劃。
- Supabase 表沿用 `app_secrets` 的慣例：啟用 RLS、但不建任何 policy，只有拿 `SUPABASE_SECRET_KEY` 的後端能讀寫；資料歸屬與權限完全由後端 API 檢查 `request.state.user_id`/`request.state.user_email`，不依賴 RLS policy。
- 測試新的 API 端點時，**不要用 `TestClient(app)` 啟動整個 app**——`api.py` 的 `lifespan` 會嘗試連線真實群益 COM（只有 Windows 硬體環境可用）。比照 `tests/test_api_auth.py` 的做法：直接呼叫 route handler function，用 `starlette.requests.Request` 建假的 request。
- 前端新頁面沿用 `frontend/backtest.html` 的骨架（`css/dashboard.css`、`js/auth.js` 的 `requireAuth()`/`apiFetch()`），不要另外發明一套樣式或驗證邏輯。

---

## File Structure

- Create: `supabase/sql/2026-08-26_user_strategy_configs.sql` — 資料表 DDL + 幫「這個帳號」種入現有 4 個策略的初始資料。手動在 Supabase SQL Editor 執行（這個專案沒有正式 migration 工具，`app_secrets` 也是這樣手動建的）。
- Create: `backend/strategy_config_store.py` — 對 `user_strategy_configs` 表的讀寫。
- Create: `tests/test_strategy_config_store.py`
- Modify: `backend/api.py` — middleware 掛 `request.state.user_id`/`user_email`；新增 admin 判斷 + 5 個端點。
- Modify: `tests/test_api_auth.py` — 補一個測試確認 middleware 有掛 `user_id`/`user_email`。
- Create: `tests/test_api_strategy_config.py`
- Create: `frontend/strategy-config.html`
- Create: `frontend/js/strategy-config.js`
- Modify: `frontend/index.html`、`frontend/backtest.html` — 加導覽連結。
- Modify: `.env`（本機）— 加 `ADMIN_EMAILS`（你自己的登入信箱）。

---

### Task 1: Supabase 資料表 + 種入現有資料

**Files:**
- Create: `supabase/sql/2026-08-26_user_strategy_configs.sql`

**Interfaces:**
- Produces: 表 `public.user_strategy_configs`，欄位 `id`(uuid pk) / `user_id`(uuid) / `strategy_id`(text) / `product_code`(text) / `qty`(integer, nullable) / `enabled`(boolean) / `created_at`/`updated_at`(timestamptz)，`unique(user_id, strategy_id)`。

- [ ] **Step 1: 寫 DDL + 種資料的 SQL**

```sql
-- supabase/sql/2026-08-26_user_strategy_configs.sql
-- 帳號 ↔ 策略配對：一般使用者的策略欄位預設是空的，由管理員手動客製化綁定
-- （見 docs/superpowers/plans/2026-08-26-account-strategy-pairing.md）。
create table if not exists public.user_strategy_configs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  strategy_id text not null,
  product_code text not null default 'TM2608',
  qty integer,
  enabled boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, strategy_id)
);

alter table public.user_strategy_configs enable row level security;
-- 刻意不建任何 policy：跟 app_secrets 一樣，只有拿 service_role/
-- SUPABASE_SECRET_KEY 的後端能讀寫，前端連不到這張表。資料歸屬跟權限完全靠
-- backend/api.py 把關（一般使用者只能讀/切換自己的列，新增/改參數限管理員），
-- 不靠 RLS policy。

-- 把「這個帳號」現在已經在跑的 4 個策略種成初始資料，代表現況。
-- ⚠️ 執行前，把下面的 '<YOUR_USER_ID>' 換成你自己的 Supabase user id
--    （Supabase Studio → Authentication → Users，複製你自己那一列的 UID；
--    或是先登入一次前端，用瀏覽器開發者工具打 GET /api/me 也看得到）。
insert into public.user_strategy_configs (user_id, strategy_id, product_code, qty, enabled)
values
  ('<YOUR_USER_ID>', 'breakout_long', 'TM2608', 1, true),
  ('<YOUR_USER_ID>', 'breakout_short', 'TM2608', 1, true),
  ('<YOUR_USER_ID>', 'pullback_long', 'TM2608', 1, true),
  ('<YOUR_USER_ID>', 'pullback_short', 'TM2608', 1, true)
on conflict (user_id, strategy_id) do nothing;
```

- [ ] **Step 2: 在 Supabase Studio 找到自己的 user id，取代 SQL 裡的 `<YOUR_USER_ID>`**

Supabase 專案（`SUPABASE_URL` 對應的那個）→ Authentication → Users，找到你自己登入用的那個帳號，複製 UID（一串 uuid），取代 Step 1 SQL 裡兩處 `<YOUR_USER_ID>`。

- [ ] **Step 3: 在 SQL Editor 執行**

SQL Editor → 貼上取代好的內容 → Run。

- [ ] **Step 4: 確認**

Table Editor 打開 `user_strategy_configs`，應該看到 4 列，`user_id` 都是你自己的 UID，`enabled` 都是 `true`。RLS 顯示已啟用、沒有 policy。

- [ ] **Step 5: Commit**

```bash
git add supabase/sql/2026-08-26_user_strategy_configs.sql
git commit -m "docs: add user_strategy_configs table DDL and seed current account's 4 strategies"
```

（`<YOUR_USER_ID>` 那兩處保留原樣提交沒關係——這份檔案是操作說明，真正執行時你會自己在 Supabase Studio 貼上取代過的版本，不需要真實 user id 進版本控管。）

---

### Task 2: `backend/strategy_config_store.py`

**Files:**
- Create: `backend/strategy_config_store.py`
- Test: `tests/test_strategy_config_store.py`

**Interfaces:**
- Consumes: `os.environ["SUPABASE_URL"]`、`os.environ["SUPABASE_SECRET_KEY"]`（已存在，見 `backend/secrets_store.py`）。
- Produces:
  - `list_user_strategy_configs(user_id: str, *, timeout: float = 10.0) -> list[dict]` — 這個使用者已經被綁定的策略列（可能是空 list）。
  - `set_strategy_enabled(user_id: str, strategy_id: str, enabled: bool, *, timeout: float = 10.0) -> dict | None` — 只切換「已存在」那一列的 `enabled`；回傳 `None` 代表這個使用者根本沒有這個 `strategy_id`（還沒被客製化過），呼叫端要當 404 處理。
  - `admin_upsert_strategy_config(user_id: str, strategy_id: str, *, product_code: str, qty: int | None, enabled: bool, timeout: float = 10.0) -> dict` — 管理員專用：新增或覆蓋任一使用者的一列設定。未設定 Supabase 環境變數時 raise `RuntimeError`。

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_strategy_config_store.py
"""backend/strategy_config_store.py 的單元測試：跟 test_secrets_store.py 用
同一套 mock requests 的模式，不打真的 Supabase。
"""

from unittest.mock import MagicMock, patch

import pytest

from backend.strategy_config_store import (
    admin_upsert_strategy_config,
    list_user_strategy_configs,
    set_strategy_enabled,
)


def test_list_returns_empty_when_not_configured(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)

    assert list_user_strategy_configs("user-123") == []


def test_list_returns_rows_on_success(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")

    fake_response = MagicMock()
    fake_response.json.return_value = [{"strategy_id": "breakout_long", "enabled": True}]
    fake_response.raise_for_status.return_value = None

    with patch("backend.strategy_config_store.requests.get", return_value=fake_response) as mock_get:
        rows = list_user_strategy_configs("user-123")

    assert rows == [{"strategy_id": "breakout_long", "enabled": True}]
    called_url = mock_get.call_args.args[0]
    assert called_url == "https://example.supabase.co/rest/v1/user_strategy_configs"
    assert mock_get.call_args.kwargs["params"]["user_id"] == "eq.user-123"


def test_list_returns_empty_on_request_failure(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")

    with patch("backend.strategy_config_store.requests.get", side_effect=RuntimeError("網路錯誤")):
        assert list_user_strategy_configs("user-123") == []


def test_set_enabled_returns_none_when_row_does_not_exist(monkeypatch):
    # 這個使用者根本沒被客製化過這個策略——PostgREST 對「沒有任何列符合篩選
    # 條件」的 PATCH 回傳 200 + 空陣列，不是錯誤，所以用回傳值分辨，不是例外。
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")

    fake_response = MagicMock()
    fake_response.json.return_value = []
    fake_response.raise_for_status.return_value = None

    with patch("backend.strategy_config_store.requests.patch", return_value=fake_response):
        result = set_strategy_enabled("user-123", "breakout_long", True)

    assert result is None


def test_set_enabled_updates_existing_row(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")

    fake_response = MagicMock()
    fake_response.json.return_value = [
        {"user_id": "user-123", "strategy_id": "breakout_long", "enabled": False}
    ]
    fake_response.raise_for_status.return_value = None

    with patch("backend.strategy_config_store.requests.patch", return_value=fake_response) as mock_patch:
        result = set_strategy_enabled("user-123", "breakout_long", False)

    assert result == {"user_id": "user-123", "strategy_id": "breakout_long", "enabled": False}
    assert mock_patch.call_args.kwargs["params"]["user_id"] == "eq.user-123"
    assert mock_patch.call_args.kwargs["params"]["strategy_id"] == "eq.breakout_long"
    assert mock_patch.call_args.kwargs["json"] == {"enabled": False}


def test_admin_upsert_raises_when_not_configured(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)

    with pytest.raises(RuntimeError):
        admin_upsert_strategy_config(
            "user-123", "breakout_long", product_code="TM2608", qty=1, enabled=False,
        )


def test_admin_upsert_posts_correct_payload_and_returns_row(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")

    fake_response = MagicMock()
    fake_response.json.return_value = [
        {"strategy_id": "breakout_long", "user_id": "user-456", "enabled": False, "qty": 2}
    ]
    fake_response.raise_for_status.return_value = None

    with patch("backend.strategy_config_store.requests.post", return_value=fake_response) as mock_post:
        row = admin_upsert_strategy_config(
            "user-456", "breakout_long", product_code="TM2608", qty=2, enabled=False,
        )

    assert row["qty"] == 2
    called_url = mock_post.call_args.args[0]
    assert called_url == "https://example.supabase.co/rest/v1/user_strategy_configs"
    payload = mock_post.call_args.kwargs["json"]
    assert payload == {
        "user_id": "user-456",
        "strategy_id": "breakout_long",
        "product_code": "TM2608",
        "qty": 2,
        "enabled": False,
    }
    assert mock_post.call_args.kwargs["params"]["on_conflict"] == "user_id,strategy_id"
```

- [ ] **Step 2: 執行確認失敗**

Run: `pytest tests/test_strategy_config_store.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'backend.strategy_config_store'`）

- [ ] **Step 3: 寫實作**

```python
# backend/strategy_config_store.py
"""backend/strategy_config_store.py — 帳號 ↔ 策略客製化綁定（Supabase
user_strategy_configs 表）。一般使用者的策略欄位預設是空的，只有管理員能新增
一列（把某個策略綁給某個帳號）；使用者自己只能切換「已經綁定給他」那幾列的
enabled（見 docs/superpowers/plans/2026-08-26-account-strategy-pairing.md）。

跟 secrets_store.py 用同一套輕量 REST 呼叫模式（不用完整 supabase-py SDK），
一律用 SUPABASE_SECRET_KEY 繞過 RLS——這張表沒有建任何 RLS policy，安全邊界
完全靠後端 API（backend/api.py 的 request.state.user_id/user_email + admin
判斷）把關，不是 Supabase 自己擋。這裡只負責讀寫，不驗證呼叫者是誰、有沒有
管理權限。
"""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)

TABLE = "user_strategy_configs"


def _base_url() -> str:
    return os.getenv("SUPABASE_URL", "").strip().rstrip("/")


def _configured() -> bool:
    return bool(_base_url() and os.getenv("SUPABASE_SECRET_KEY", "").strip())


def _headers() -> dict[str, str]:
    key = os.getenv("SUPABASE_SECRET_KEY", "").strip()
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def list_user_strategy_configs(user_id: str, *, timeout: float = 10.0) -> list[dict]:
    """回傳這個使用者已經被客製化綁定的策略列；沒有任何綁定（新帳號的常態）
    就是空 list，不是錯誤。未設定 Supabase 或連線失敗都安靜回傳空 list。"""
    if not _configured():
        return []
    try:
        resp = requests.get(
            f"{_base_url()}/rest/v1/{TABLE}",
            headers=_headers(),
            params={"user_id": f"eq.{user_id}", "select": "*"},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("讀取使用者策略設定失敗: %s", exc)
        return []


def set_strategy_enabled(
    user_id: str, strategy_id: str, enabled: bool, *, timeout: float = 10.0
) -> dict | None:
    """一般使用者的自助動作：只切換「已經存在」那一列的 enabled，不會建立新列
    ——新增綁定是管理員的事（見 admin_upsert_strategy_config）。回傳 None 代表
    這個使用者根本沒有這個 strategy_id 的設定，呼叫端要當 404 處理。"""
    if not _configured():
        raise RuntimeError("SUPABASE_URL/SUPABASE_SECRET_KEY 未設定，無法更新策略設定")
    resp = requests.patch(
        f"{_base_url()}/rest/v1/{TABLE}",
        headers={**_headers(), "Prefer": "return=representation"},
        params={"user_id": f"eq.{user_id}", "strategy_id": f"eq.{strategy_id}"},
        json={"enabled": enabled},
        timeout=timeout,
    )
    resp.raise_for_status()
    rows = resp.json()
    return rows[0] if rows else None


def admin_upsert_strategy_config(
    user_id: str,
    strategy_id: str,
    *,
    product_code: str,
    qty: int | None,
    enabled: bool,
    timeout: float = 10.0,
) -> dict:
    """管理員專用：把某個策略客製化綁定給某個帳號（新增），或更新已經綁定過
    的參數；靠 (user_id, strategy_id) 的 unique 限制做 upsert。呼叫端
    （backend/api.py）要先驗證呼叫者是管理員，這裡不做這層檢查。失敗直接
    raise，讓 API 層回 400——管理員主動送出的設定，存不進去要讓他知道。"""
    if not _configured():
        raise RuntimeError("SUPABASE_URL/SUPABASE_SECRET_KEY 未設定，無法儲存策略設定")

    payload = {
        "user_id": user_id,
        "strategy_id": strategy_id,
        "product_code": product_code,
        "qty": qty,
        "enabled": enabled,
    }
    headers = _headers()
    headers["Prefer"] = "resolution=merge-duplicates,return=representation"
    resp = requests.post(
        f"{_base_url()}/rest/v1/{TABLE}",
        headers=headers,
        params={"on_conflict": "user_id,strategy_id"},
        json=payload,
        timeout=timeout,
    )
    resp.raise_for_status()
    rows = resp.json()
    return rows[0] if rows else payload
```

- [ ] **Step 4: 執行確認通過**

Run: `pytest tests/test_strategy_config_store.py -v`
Expected: 7 個測試全部 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/strategy_config_store.py tests/test_strategy_config_store.py
git commit -m "feat: add per-user strategy config store with admin-only creation"
```

---

### Task 3: middleware 掛身分，新增 `GET /api/me`（含 `is_admin`）

**Files:**
- Modify: `backend/api.py:128-146`（`require_supabase_auth`）、新增 `/api/me` 端點與 `_is_admin` helper
- Modify: `tests/test_api_auth.py`
- Modify: `.env`（本機）

**Interfaces:**
- Consumes: `verify_supabase_jwt(token) -> dict`（既有，claims 含 `sub`/`email`）；`os.environ["ADMIN_EMAILS"]`（新增，逗號分隔）。
- Produces: `request.state.user_id: str`、`request.state.user_email: str`；`_is_admin(request: Request) -> bool`；`GET /api/me -> {"user_id": str, "email": str, "is_admin": bool}`。

- [ ] **Step 1: 補 middleware 測試（先寫會失敗的）**

在 `tests/test_api_auth.py` 檔案末尾加入：

```python
@pytest.mark.anyio
async def test_valid_token_attaches_user_id_and_email_to_request_state():
    request = _make_request(
        "/api/positions",
        headers=[(b"authorization", b"Bearer valid-token-here")],
    )
    call_next = AsyncMock(return_value="ok-response")

    with patch(
        "backend.api.verify_supabase_jwt",
        return_value={"sub": "user-123", "email": "a@example.com"},
    ):
        await require_supabase_auth(request, call_next)

    assert request.state.user_id == "user-123"
    assert request.state.user_email == "a@example.com"
```

- [ ] **Step 2: 執行確認失敗**

Run: `pytest tests/test_api_auth.py::test_valid_token_attaches_user_id_and_email_to_request_state -v`
Expected: FAIL（`AttributeError: 'State' object has no attribute 'user_id'`）

- [ ] **Step 3: 修改 middleware**

`backend/api.py:141-146` 現在是：

```python
    try:
        verify_supabase_jwt(token or "")
    except InvalidSupabaseToken:
        return JSONResponse({"detail": "請先登入"}, status_code=401)

    return await call_next(request)
```

改成：

```python
    try:
        claims = verify_supabase_jwt(token or "")
    except InvalidSupabaseToken:
        return JSONResponse({"detail": "請先登入"}, status_code=401)

    # 2026-08-26：多帳號規劃第一步——把登入者身分掛到 request.state，讓後面
    # 個人化/權限判斷的端點（/api/my-strategy-configs、/api/admin/* 等）知道
    # 「這是誰、是不是管理員」，不用每個 handler 自己重新解一次 JWT（見
    # docs/superpowers/plans/2026-08-26-account-strategy-pairing.md）。
    request.state.user_id = claims.get("sub", "")
    request.state.user_email = claims.get("email", "")
    return await call_next(request)
```

- [ ] **Step 4: 執行確認通過**

Run: `pytest tests/test_api_auth.py -v`
Expected: 全部（含新增這個）PASS

- [ ] **Step 5: 加 `_is_admin` helper + `GET /api/me`**

在 `backend/api.py` 的 `/api/health` 端點（約第 431-433 行）之後加入：

```python
def _is_admin(request: Request) -> bool:
    """現階段只用一個環境變數做管理員清單（逗號分隔的信箱），不建角色資料表
    ——只有你一個人是管理員，沒必要為此多一張表。之後真的有多個管理員/更細
    的權限需求，再另外規劃。"""
    email = getattr(request.state, "user_email", "").strip().lower()
    if not email:
        return False
    admin_emails = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}
    return email in admin_emails


@app.get("/api/me")
def api_me(request: Request) -> dict[str, object]:
    return {
        "user_id": getattr(request.state, "user_id", ""),
        "email": getattr(request.state, "user_email", ""),
        "is_admin": _is_admin(request),
    }
```

- [ ] **Step 6: 加 `.env` 設定**

本機 `.env` 加一行（換成你自己登入用的信箱；多個管理員用逗號分隔）：

```
ADMIN_EMAILS=你的登入信箱@example.com
```

- [ ] **Step 7: 寫測試確認**

新增 `tests/test_api_strategy_config.py`（這個檔案接下來 Task 4、5 還會繼續加測試，先建立骨架）：

```python
"""backend/api.py 新增的帳號↔策略設定端點測試。跟 test_api_auth.py 一樣，不用
TestClient 啟動整個 app（lifespan 會嘗試連線真實群益 COM），直接呼叫 route
handler function，用假的 Request 帶 request.state.user_id/user_email。
"""

from unittest.mock import patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from backend.api import api_me


def _make_request(*, user_id: str = "", user_email: str = "") -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/me",
        "query_string": b"",
        "headers": [],
        "client": ("test", 0),
    }
    request = Request(scope)
    request.state.user_id = user_id
    request.state.user_email = user_email
    return request


def test_api_me_returns_identity_and_admin_flag(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    request = _make_request(user_id="user-123", user_email="boss@example.com")

    result = api_me(request)

    assert result == {"user_id": "user-123", "email": "boss@example.com", "is_admin": True}


def test_api_me_non_admin_user(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    request = _make_request(user_id="user-456", user_email="someone-else@example.com")

    result = api_me(request)

    assert result == {"user_id": "user-456", "email": "someone-else@example.com", "is_admin": False}
```

- [ ] **Step 8: 執行確認通過**

Run: `pytest tests/test_api_strategy_config.py -v`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add backend/api.py tests/test_api_auth.py tests/test_api_strategy_config.py
git commit -m "feat: attach authenticated identity to request state, add /api/me with admin flag"
```

（`.env` 是本機檔案，不進版本控管，不用 commit。）

---

### Task 4: 管理員專用 `GET /api/admin/strategy-defs`、`POST /api/admin/strategy-configs`

**Files:**
- Modify: `backend/api.py`（import 區塊 + 兩個新端點）
- Modify: `tests/test_api_strategy_config.py`

**Interfaces:**
- Consumes: `STRATEGY_DEFS`（既有）、`strategy_config_defaults(strategy_id)`（既有）、`_is_admin(request)`（Task 3 產出）、`admin_upsert_strategy_config(...)`（Task 2 產出）。
- Produces: `GET /api/admin/strategy-defs -> list[dict]`（403 若非管理員）；`POST /api/admin/strategy-configs`（body: `AdminStrategyConfigRequest`）`-> dict`（403 若非管理員）。

- [ ] **Step 1: 補 import**

`backend/api.py:50-60` 現在的 `.strategy_service` import 是：

```python
from .strategy_service import (
    STRATEGY_DEFS,
    klines_with_signals,
    reconcile_after_manual_close,
    start_strategy,
    stop_strategy,
    strategy_list,
    strategy_status,
    strategy_tick,
    trade_log,
)
```

改成（加入 `strategy_config_defaults`）：

```python
from .strategy_service import (
    STRATEGY_DEFS,
    klines_with_signals,
    reconcile_after_manual_close,
    start_strategy,
    stop_strategy,
    strategy_config_defaults,
    strategy_list,
    strategy_status,
    strategy_tick,
    trade_log,
)
```

再加入新的 import（跟上面相鄰即可）：

```python
from .strategy_config_store import admin_upsert_strategy_config, list_user_strategy_configs, set_strategy_enabled
```

- [ ] **Step 2: 寫失敗測試**

加到 `tests/test_api_strategy_config.py`（更新開頭 import 為：`from backend.api import AdminStrategyConfigRequest, api_admin_strategy_defs, api_admin_upsert_strategy_config, api_me`）：

```python
def test_admin_strategy_defs_rejects_non_admin(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    request = _make_request(user_id="user-456", user_email="someone-else@example.com")

    with pytest.raises(HTTPException) as exc_info:
        api_admin_strategy_defs(request)
    assert exc_info.value.status_code == 403


def test_admin_strategy_defs_lists_all_four_for_admin(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    request = _make_request(user_id="admin-1", user_email="boss@example.com")

    catalog = api_admin_strategy_defs(request)

    ids = {row["strategy_id"] for row in catalog}
    assert ids == {"breakout_long", "breakout_short", "pullback_long", "pullback_short"}


def test_admin_upsert_rejects_non_admin(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    request = _make_request(user_id="user-456", user_email="someone-else@example.com")
    body = AdminStrategyConfigRequest(user_id="user-789", strategy_id="breakout_long")

    with pytest.raises(HTTPException) as exc_info:
        api_admin_upsert_strategy_config(request, body)
    assert exc_info.value.status_code == 403


def test_admin_upsert_rejects_unknown_strategy_id(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    request = _make_request(user_id="admin-1", user_email="boss@example.com")
    body = AdminStrategyConfigRequest(user_id="user-789", strategy_id="not_real")

    with pytest.raises(HTTPException) as exc_info:
        api_admin_upsert_strategy_config(request, body)
    assert exc_info.value.status_code == 400


def test_admin_upsert_calls_store_for_target_user(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    request = _make_request(user_id="admin-1", user_email="boss@example.com")
    body = AdminStrategyConfigRequest(
        user_id="user-789", strategy_id="breakout_long", product_code="TM2608", qty=3, enabled=False,
    )

    with patch(
        "backend.api.admin_upsert_strategy_config",
        return_value={"strategy_id": "breakout_long", "user_id": "user-789"},
    ) as mock_upsert:
        result = api_admin_upsert_strategy_config(request, body)

    mock_upsert.assert_called_once_with(
        "user-789", "breakout_long", product_code="TM2608", qty=3, enabled=False,
    )
    assert result == {"strategy_id": "breakout_long", "user_id": "user-789"}
```

- [ ] **Step 3: 執行確認失敗**

Run: `pytest tests/test_api_strategy_config.py -v`
Expected: 新增的測試 FAIL（`ImportError`）

- [ ] **Step 4: 加 Pydantic model + 端點**

在 `backend/api.py` 的 `StrategyStopRequest`（約第 203-205 行）之後加入：

```python
class AdminStrategyConfigRequest(BaseModel):
    user_id: str
    strategy_id: str
    product_code: str = "TM2608"
    qty: int | None = None
    enabled: bool = False


class ToggleStrategyRequest(BaseModel):
    strategy_id: str
    enabled: bool
```

在 `api_me`（Task 3 加的）之後加入：

```python
@app.get("/api/admin/strategy-defs")
def api_admin_strategy_defs(request: Request) -> list[dict[str, object]]:
    """管理員專用參考清單：目前可以綁定的策略（仍然是 STRATEGY_DEFS 固定的
    4 個，還沒有動態新增策略的機制——新增策略仍然要改程式碼部署）。"""
    if not _is_admin(request):
        raise HTTPException(status_code=403, detail="沒有管理權限")
    catalog: list[dict[str, object]] = []
    for strategy_id in STRATEGY_DEFS:
        defaults = strategy_config_defaults(strategy_id)
        catalog.append(
            {
                "strategy_id": strategy_id,
                "label": defaults["label"],
                "strategy": defaults["strategy"],
                "direction_limit": defaults["direction_limit"],
                "default_qty": defaults["qty"],
            }
        )
    return catalog


@app.post("/api/admin/strategy-configs")
def api_admin_upsert_strategy_config(
    request: Request, body: AdminStrategyConfigRequest
) -> dict[str, object]:
    """把某個策略客製化綁定給某個使用者（新增或更新參數）。這是唯一能讓某個
    帳號的策略欄位「從空變成有東西」的路徑，一般使用者自己沒有這個能力。"""
    if not _is_admin(request):
        raise HTTPException(status_code=403, detail="沒有管理權限")
    if body.strategy_id not in STRATEGY_DEFS:
        raise HTTPException(status_code=400, detail=f"未知的策略 id: {body.strategy_id}")
    try:
        return admin_upsert_strategy_config(
            body.user_id,
            body.strategy_id,
            product_code=body.product_code,
            qty=body.qty,
            enabled=body.enabled,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
```

- [ ] **Step 5: 執行確認通過**

Run: `pytest tests/test_api_strategy_config.py -v`
Expected: 全部 PASS

- [ ] **Step 6: Commit**

```bash
git add backend/api.py tests/test_api_strategy_config.py
git commit -m "feat: add admin-only endpoints to bind strategies to a target account"
```

---

### Task 5: 一般使用者 `GET /api/my-strategy-configs`、`POST /api/my-strategy-configs/toggle`

**Files:**
- Modify: `backend/api.py`
- Modify: `tests/test_api_strategy_config.py`

**Interfaces:**
- Consumes: `list_user_strategy_configs(user_id)`、`set_strategy_enabled(user_id, strategy_id, enabled)`（Task 2 產出）；`request.state.user_id`（Task 3 產出）；`ToggleStrategyRequest`（Task 4 產出）。
- Produces: `GET /api/my-strategy-configs -> list[dict]`（只有呼叫者自己的列，可能是空 list）；`POST /api/my-strategy-configs/toggle -> dict`（404 若這個使用者沒有這個 `strategy_id`）。

- [ ] **Step 1: 寫失敗測試**

加到 `tests/test_api_strategy_config.py`（更新開頭 import，加入 `api_my_strategy_configs, api_toggle_my_strategy_config, ToggleStrategyRequest`）：

```python
def test_my_strategy_configs_requires_login():
    request = _make_request(user_id="")

    with pytest.raises(HTTPException) as exc_info:
        api_my_strategy_configs(request)
    assert exc_info.value.status_code == 401


def test_my_strategy_configs_returns_only_this_users_rows():
    request = _make_request(user_id="user-123")

    with patch(
        "backend.api.list_user_strategy_configs",
        return_value=[{"strategy_id": "breakout_long", "enabled": True}],
    ) as mock_list:
        result = api_my_strategy_configs(request)

    mock_list.assert_called_once_with("user-123")
    assert result == [{"strategy_id": "breakout_long", "enabled": True}]


def test_my_strategy_configs_empty_for_account_with_no_custom_strategy():
    # 新帳號、還沒被管理員客製化過任何策略——回傳空 list，不是錯誤。
    request = _make_request(user_id="brand-new-user")

    with patch("backend.api.list_user_strategy_configs", return_value=[]):
        result = api_my_strategy_configs(request)

    assert result == []


def test_toggle_requires_login():
    request = _make_request(user_id="")
    body = ToggleStrategyRequest(strategy_id="breakout_long", enabled=True)

    with pytest.raises(HTTPException) as exc_info:
        api_toggle_my_strategy_config(request, body)
    assert exc_info.value.status_code == 401


def test_toggle_returns_404_when_not_customized_for_this_user():
    request = _make_request(user_id="user-123")
    body = ToggleStrategyRequest(strategy_id="breakout_long", enabled=True)

    with patch("backend.api.set_strategy_enabled", return_value=None) as mock_set:
        with pytest.raises(HTTPException) as exc_info:
            api_toggle_my_strategy_config(request, body)

    mock_set.assert_called_once_with("user-123", "breakout_long", True)
    assert exc_info.value.status_code == 404


def test_toggle_updates_existing_row():
    request = _make_request(user_id="user-123")
    body = ToggleStrategyRequest(strategy_id="breakout_long", enabled=False)

    with patch(
        "backend.api.set_strategy_enabled",
        return_value={"strategy_id": "breakout_long", "enabled": False},
    ) as mock_set:
        result = api_toggle_my_strategy_config(request, body)

    mock_set.assert_called_once_with("user-123", "breakout_long", False)
    assert result == {"strategy_id": "breakout_long", "enabled": False}
```

- [ ] **Step 2: 執行確認失敗**

Run: `pytest tests/test_api_strategy_config.py -v`
Expected: 新增的測試 FAIL（`ImportError`）

- [ ] **Step 3: 加端點**

在 `api_admin_upsert_strategy_config`（Task 4 加的）之後加入：

```python
@app.get("/api/my-strategy-configs")
def api_my_strategy_configs(request: Request) -> list[dict[str, object]]:
    """只回傳呼叫者自己被客製化過的策略列——新帳號常態是空 list，不代表出錯，
    前端要顯示「還沒有任何策略，請聯絡管理員」這種空狀態，不是自己生一份預設清單。"""
    user_id = getattr(request.state, "user_id", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="請先登入")
    return list_user_strategy_configs(user_id)


@app.post("/api/my-strategy-configs/toggle")
def api_toggle_my_strategy_config(
    request: Request, body: ToggleStrategyRequest
) -> dict[str, object]:
    """使用者自助動作：只能切換「已經綁定給自己」的策略開關，不能新增策略或
    改參數（那是管理員的事，見 /api/admin/strategy-configs）。"""
    user_id = getattr(request.state, "user_id", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="請先登入")
    try:
        row = set_strategy_enabled(user_id, body.strategy_id, body.enabled)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(
            status_code=404, detail="這個帳號還沒有這個策略的設定，請聯絡管理員幫你客製化"
        )
    return row
```

- [ ] **Step 4: 執行確認通過**

Run: `pytest tests/test_api_strategy_config.py tests/test_api_auth.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 跑整個後端測試套件，確認沒改壞既有東西**

Run: `pytest tests/ -v`
Expected: 全部 PASS（除了少數已知需要 Windows/群益 COM 硬體環境才能跑的測試，跟這次改動無關）

- [ ] **Step 6: Commit**

```bash
git add backend/api.py tests/test_api_strategy_config.py
git commit -m "feat: add self-service strategy toggle endpoint scoped to caller's own rows"
```

---

### Task 6: 前端「我的策略」頁面（一般使用者檢視/開關 + 管理員客製化面板）

**Files:**
- Create: `frontend/strategy-config.html`
- Create: `frontend/js/strategy-config.js`
- Modify: `frontend/index.html`（加導覽連結）
- Modify: `frontend/backtest.html`（加導覽連結）

**Interfaces:**
- Consumes: `requireAuth()`/`apiFetch(url, options)`（`frontend/js/auth.js`，既有）；`GET /api/me`、`GET /api/my-strategy-configs`、`POST /api/my-strategy-configs/toggle`、`GET /api/admin/strategy-defs`（僅 admin）、`POST /api/admin/strategy-configs`（僅 admin）（Task 3-5 產出）。

- [ ] **Step 1: 建立頁面骨架**

```html
<!-- frontend/strategy-config.html -->
<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>我的策略</title>
  <script src="js/supabase.js?v=1"></script>
  <script src="js/auth.js?v=3"></script>
  <link rel="stylesheet" href="css/dashboard.css?v=4" />
</head>
<body>
  <div class="app-shell">
    <header class="page-header">
      <div>
        <p class="eyebrow">Account · Strategy Pairing</p>
        <h1>我的策略</h1>
      </div>
      <div class="header-status">
        <a href="index.html" class="header-link">← 實盤交易 Dashboard</a>
        <a href="backtest.html" class="header-link">回測</a>
        <button type="button" class="header-link" onclick="logout()" style="cursor:pointer">登出</button>
        <span id="me-email" class="status-pill">—</span>
      </div>
    </header>

    <section id="error-banner" class="error-banner hidden"></section>

    <section class="panel">
      <div class="panel-heading">
        <div>
          <h2>已經綁定給我的策略</h2>
          <p>只能開關，不能改參數——參數要請管理員幫你調整。</p>
        </div>
      </div>
      <div id="my-strategy-list"></div>
      <p id="my-strategy-empty" class="dataset-meta hidden">目前還沒有任何策略綁定給這個帳號，請聯絡管理員幫你客製化。</p>
    </section>

    <section id="admin-panel" class="panel hidden">
      <div class="panel-heading">
        <div>
          <h2>管理員：幫某個帳號客製化策略</h2>
          <p>使用者的 user id 去 Supabase Studio → Authentication → Users 複製。</p>
        </div>
      </div>
      <div class="control-grid">
        <label class="field">
          <span>目標使用者 user id</span>
          <input type="text" id="admin-user-id" placeholder="uuid" />
        </label>
        <label class="field">
          <span>策略</span>
          <select id="admin-strategy-select"></select>
        </label>
        <label class="field">
          <span>商品代碼</span>
          <input type="text" id="admin-product-code" value="TM2608" />
        </label>
        <label class="field">
          <span>口數</span>
          <input type="number" id="admin-qty" min="1" value="1" />
        </label>
        <label class="field checkbox-field">
          <input type="checkbox" id="admin-enabled" />
          <span>建立後直接啟用</span>
        </label>
      </div>
      <div class="control-actions">
        <button type="button" class="primary-button" id="admin-save-btn">儲存客製化設定</button>
      </div>
    </section>
  </div>

  <script src="js/strategy-config.js?v=1"></script>
</body>
</html>
```

- [ ] **Step 2: 寫互動邏輯**

```javascript
// frontend/js/strategy-config.js — 帳號↔策略配對第一步（見
// docs/superpowers/plans/2026-08-26-account-strategy-pairing.md）：
// 一般使用者只能看/開關「已經綁定給自己」的策略；管理員（is_admin）另外看到
// 一個面板，可以幫任何帳號新增/更新策略客製化設定。enabled 目前只是記錄
// 使用者的意圖，還不會觸發多帳號並發下單——那是後續計畫（Plan B），要等
// 「一台機器同時並發多個不同帳號 SKCOM 連線」實測確認可行之後才會做。

const STRATEGY_DIRECTION_LABEL = { long: '只做多', short: '只做空' };
const STRATEGY_KIND_LABEL = { breakout: '突破', pullback: '拉回' };

function rowHtml(row) {
  return `
    <div class="strategy-card" data-strategy-id="${row.strategy_id}">
      <strong>${row.strategy_id}</strong>
      <span class="stat-label">商品代碼 ${row.product_code}　口數 ${row.qty}</span>
      <label class="field checkbox-field">
        <input type="checkbox" class="row-enabled" ${row.enabled ? 'checked' : ''} />
        <span>啟用</span>
      </label>
    </div>
  `;
}

async function loadMyStrategies() {
  const res = await apiFetch('/api/my-strategy-configs');
  const rows = await res.json();

  const listEl = document.getElementById('my-strategy-list');
  const emptyEl = document.getElementById('my-strategy-empty');
  if (rows.length === 0) {
    listEl.innerHTML = '';
    emptyEl.classList.remove('hidden');
    return;
  }
  emptyEl.classList.add('hidden');
  listEl.innerHTML = rows.map(rowHtml).join('');
  listEl.querySelectorAll('.strategy-card').forEach((card) => {
    card.querySelector('.row-enabled').addEventListener('change', (evt) => {
      toggleStrategy(card.dataset.strategyId, evt.target.checked, evt.target);
    });
  });
}

async function toggleStrategy(strategyId, enabled, checkboxEl) {
  const errorBanner = document.getElementById('error-banner');
  errorBanner.classList.add('hidden');
  try {
    const res = await apiFetch('/api/my-strategy-configs/toggle', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ strategy_id: strategyId, enabled }),
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || '更新失敗');
    }
  } catch (e) {
    checkboxEl.checked = !enabled; // 失敗要把畫面狀態改回去，不能讓使用者以為切換成功了
    errorBanner.textContent = e.message;
    errorBanner.classList.remove('hidden');
  }
}

async function setupAdminPanel() {
  document.getElementById('admin-panel').classList.remove('hidden');

  const defsRes = await apiFetch('/api/admin/strategy-defs');
  const defs = await defsRes.json();
  const select = document.getElementById('admin-strategy-select');
  select.innerHTML = defs
    .map((d) => `<option value="${d.strategy_id}">${d.label}（${STRATEGY_KIND_LABEL[d.strategy]}／${STRATEGY_DIRECTION_LABEL[d.direction_limit]}）</option>`)
    .join('');

  document.getElementById('admin-save-btn').addEventListener('click', async () => {
    const errorBanner = document.getElementById('error-banner');
    errorBanner.classList.add('hidden');
    const body = {
      user_id: document.getElementById('admin-user-id').value.trim(),
      strategy_id: select.value,
      product_code: document.getElementById('admin-product-code').value.trim(),
      qty: parseInt(document.getElementById('admin-qty').value, 10),
      enabled: document.getElementById('admin-enabled').checked,
    };
    try {
      const res = await apiFetch('/api/admin/strategy-configs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || '儲存失敗');
      }
      await loadMyStrategies(); // 萬一管理員幫自己帳號改設定，畫面要同步
    } catch (e) {
      errorBanner.textContent = e.message;
      errorBanner.classList.remove('hidden');
    }
  });
}

async function init() {
  const token = await requireAuth();
  if (!token) return;

  const meRes = await apiFetch('/api/me');
  const me = await meRes.json();
  document.getElementById('me-email').textContent = me.email || me.user_id || '—';

  await loadMyStrategies();
  if (me.is_admin) {
    await setupAdminPanel();
  }
}

init().catch((e) => {
  const errorBanner = document.getElementById('error-banner');
  errorBanner.textContent = e.message;
  errorBanner.classList.remove('hidden');
});
```

- [ ] **Step 3: 加導覽連結**

`frontend/index.html:28` 的 `<a href="backtest.html" class="nav-link">回測</a>` 之後加一行：

```html
<a href="strategy-config.html" class="nav-link">我的策略</a>
```

`frontend/backtest.html` 的 `<a href="index.html" class="header-link">← 實盤交易 Dashboard</a>` 之後加一行：

```html
<a href="strategy-config.html" class="header-link">我的策略</a>
```

- [ ] **Step 4: 手動驗證（這個專案的前端沒有自動化測試，跟既有慣例一致）**

啟動後端，瀏覽器開 `http://127.0.0.1:8765/strategy-config.html`：
1. 用你自己（管理員）的帳號登入，確認看到 4 張策略卡片（Task 1 種的資料），全部顯示已啟用；同時應該看到下面多一個「管理員：幫某個帳號客製化策略」面板。
2. 試著把其中一張卡片的開關關掉，確認畫面即時反應、沒有錯誤訊息；重新整理頁面確認狀態有保留。
3. 用管理員面板，隨便填一個不存在的 user id 送出，確認 Supabase 那張表真的多了一列（因為 FK 限制 `references auth.users(id)`，如果填的 user id 在 `auth.users` 真的不存在，這裡應該會失敗——用 Supabase Studio 裡真實存在的另一個帳號 UID 測，如果暫時沒有第二個帳號，這步驟可以先跳過，等真的有第二個使用者時再驗證）。
4. 如果有第二個測試帳號，讓它登入這個頁面，確認：管理員面板不會出現（`is_admin` 是 false）；在管理員還沒幫他綁定任何策略之前，看到的是空狀態文字，不是 4 張卡片。

- [ ] **Step 5: Commit**

```bash
git add frontend/strategy-config.html frontend/js/strategy-config.js frontend/index.html frontend/backtest.html
git commit -m "feat: add strategy page with self-service toggle and admin customization panel"
```

---

## Self-Review Notes

- **Spec coverage**：「不是從 4 個策略中挑選，而是現在這個帳號綁定這 4 個策略；別的帳號沒被客製化就是空的」——Task 1 把「這個帳號」現有的 4 個策略種成初始資料；一般使用者端點（Task 5）只回傳/切換呼叫者自己已存在的列，不會合併任何預設清單，新帳號自然就是空 list；新增綁定的唯一路徑是 Task 4 的管理員端點。「使用者跟我提需求，我將策略上架，但使用者自己按啟動策略」——「上架」對應管理員的 `POST /api/admin/strategy-configs`（新增時 `enabled` 預設 `false`），「使用者自己按啟動」對應 `POST /api/my-strategy-configs/toggle`。
- **已知缺口，刻意不在這個計畫處理**：`enabled=true` 目前只是存在 Supabase，不會讓 `strategy_service.py` 真的開始跑這個使用者的策略——因為那需要先解決「一台 VM 能不能同時並發多個不同帳號的 SKCOM 連線」，而這件事目前只有一則聊天紀錄的二手確認，還沒有實測驗證過。這個計畫做完之後，下一步應該是：(1) 找一個真實的第二帳號，實測並發登入是否穩定；(2) 若可行，再寫 Plan B 把 `live_service.py`/`strategy_service.py` 從全域單例改成「每個 `user_id` 一份」，讓 `enabled=true` 真的接上執行。
- **Placeholder scan**：所有 code block 都是可以直接貼上執行的完整內容；Task 1 SQL 裡的 `<YOUR_USER_ID>` 是刻意留給操作者手動替換的真實資料（不是程式碼佔位符），Step 2 有明確指示怎麼取得。
- **Type consistency**：`strategy_config_store.py` 三個函式簽名（`list_user_strategy_configs(user_id, *, timeout=10.0)`、`set_strategy_enabled(user_id, strategy_id, enabled, *, timeout=10.0)`、`admin_upsert_strategy_config(user_id, strategy_id, *, product_code, qty, enabled, timeout=10.0)`）在 Task 2 定義，Task 4/5 的 mock 呼叫（`assert_called_once_with(...)`）用的位置/關鍵字參數一致。
