# 每帳號一個獨立 Worker 行程（Plan B-1：基礎設施，不含跨帳號路由）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓每個已經在 `user_strategy_configs` 綁定了策略的帳號，都能各自跑一個獨立的 backend 行程（自己的 SKCOM 連線、自己的持倉/委託、自己的策略執行狀態），K 線/報價仍然只由一個共用帳號的行程負責讀取與寫入（跟使用者已經確認過的設計：「k線快取用一個固定帳號，委托/持仓就綁定帳號」一致）。

**Architecture 決策與理由（先講清楚為什麼選這條路，不是憑空決定）：**

實測追出一個關鍵、之前沒人發現的技術風險：`backend/broker/capital_futures.py` 的 `CapitalFuturesBroker` 靠 `_ensure_sta_apartment()`（`pythoncom.CoInitializeEx(COINIT_APARTMENTTHREADED)`）把 COM 物件綁在**建立它的那個執行緒**上——STA（單執行緒房間）模型要求對這個物件的所有呼叫、以及它的事件回呼（`OnConnection`/`OnNotifyTicksLONG` 等）都必須在同一個執行緒上發生，且需要那個執行緒持續呼叫 `pythoncom.PumpWaitingMessages()`（`_pump_events()`）才會真的派送事件。現有單帳號程式碼能動，是因為 `poll_loop()` 每輪只**循序**呼叫一次 `asyncio.to_thread(_sync_refresh)`，前一輪還沒結束不會有下一輪，Python 預設的 `ThreadPoolExecutor` 在這種不重疊的呼叫模式下，多半會重複用同一條閒置的 worker 執行緒——這其實是隱含假設，不是保證。

如果改成「一個行程裡建立 N 個 `CapitalFuturesBroker` 執行緒」，N 個帳號各自的輪詢如果時間上重疊（多帳號本來就會），`asyncio.to_thread` 完全可能把某個帳號的呼叫派到不同的執行緒去，直接違反 STA 規則，導致難以重現、間歇性的連線/事件問題——這種坑跟你們這次 session 之前花很多力氣查的「報價 3003 間歇性卡住」是同一個風險等級的問題，不值得冒。

**因此這個計畫選「一個帳號一個獨立 OS 行程」，不是「一個行程裡多執行緒」**：每個帳號的 worker 就是現有這個 FastAPI app（`backend.api:app`，透過 `run_server.py` 或直接 `uvicorn`）用不同的環境變數重新啟動一份，跑在不同的 port 上。這樣完全繞開 STA 跨執行緒問題（每個行程有自己独立的主執行緒/事件迴圈），也讓 `strategy_service.py` 的 `_armed`（模組層級全域字典）**天生就是隔離的**——不需要把它重構成「每帳號一份」的登記表，因為每個帳號本來就是不同的 Python 行程、不同的記憶體空間。

**這個計畫刻意不做的事（Plan B-2 才做）：** 跨帳號的請求路由/閘道（讓一個網址、不同登入帳號自動連到各自的 worker port）——這個要等下面 Task 6 的手動雙開驗證，證實「一台機器真的能同時跑兩個不同帳號的 SKCOM 連線」之後才值得做，現在做等於在未驗證的地基上蓋東西。

**Tech Stack:** FastAPI/uvicorn（既有）、Supabase REST API（既有模式）、pytest + `unittest.mock`、`getpass`（CLI 密碼輸入，不落地、不進 shell history）。

## Global Constraints

- 不重構 `strategy_service.py`/`CapitalFuturesBroker` 的內部邏輯——靠「一帳號一行程」天生隔離，不是靠改寫這些模組。
- 新增的密鑰表比照 `app_secrets`/`user_strategy_configs`：啟用 RLS、不建 policy，只有拿 `SUPABASE_SECRET_KEY` 的後端能讀寫。
- 任何會經手真實密碼的地方（`scripts/set_broker_credentials.py`）一律用 `getpass.getpass()`，絕對不能是 command-line 參數（會留在 shell history/`ps`/工作管理員的行程清單裡）。
- 測試裡凡是會呼叫 `admin_upsert_strategy_config`/新的密鑰函式的，都要 mock `requests`，不能打真的 Supabase（跟本次之前 Task 2 的慣例一致）。

---

## File Structure

- Create: `supabase/sql/2026-08-26_user_broker_credentials.sql`
- Create: `backend/broker_credentials_store.py`
- Create: `tests/test_broker_credentials_store.py`
- Create: `scripts/set_broker_credentials.py`
- Modify: `backend/broker/capital_futures.py`（`_append_position_audit`/`_position_audit_log_path` 加帳號標記）
- Modify: `tests/test_capital_futures_broker.py`（補對應測試）
- Modify: `backend/live_service.py`（`ACCOUNT_USER_ID`/`WORKER_MODE` 支援）
- Create: `tests/test_live_service_worker_mode.py`
- Create: `docs/worker_process_manual_test.md`（Task 6，手動雙開驗證步驟）

---

### Task 1：`position_audit.log` 補上帳號標記

**Files:**
- Modify: `backend/broker/capital_futures.py:267-289`（`_position_audit_log_path`/`_append_position_audit`、呼叫端 `_parse_open_interest`）
- Modify: `tests/test_capital_futures_broker.py`

**Interfaces:**
- Produces: `_append_position_audit(*, account: str, raw: str, positions: list[dict]) -> None`（新增 `account` 必填關鍵字參數）。

- [ ] **Step 1: 寫失敗測試**

在 `tests/test_capital_futures_broker.py` 找到既有的 `_append_position_audit` 相關測試（`test_parse_open_interest_updates_on_non_empty` 之類），確認檔案開頭有 `_isolate_audit_logs` autouse fixture（monkeypatch 兩個 log path 函式指到 `tmp_path`），然後加入：

```python
def test_position_audit_log_includes_account_tag(tmp_path, monkeypatch):
    from backend.broker.capital_futures import CapitalFuturesBroker

    broker = CapitalFuturesBroker.__new__(CapitalFuturesBroker)  # 跳過 __init__ 的 COM 初始化
    broker.user_id = "test_user_123"
    broker.live = type("Live", (), {"positions": None})()

    broker._parse_open_interest("TXF202609,買進,1,17000")

    log_path = capital_futures._position_audit_log_path()
    content = log_path.read_text(encoding="utf-8")
    assert "account=test_user_123" in content
```

（這裡沿用既有測試檔案裡 `CapitalFuturesBroker.__new__` 跳過建構子的既有寫法，若既有測試用別的手法建立測試用 broker，改用該檔案既有的手法，不要另外發明。）

- [ ] **Step 2: 執行確認失敗**

Run: `pytest tests/test_capital_futures_broker.py::test_position_audit_log_includes_account_tag -v`
Expected: FAIL（`account=` 目前不在輸出裡）

- [ ] **Step 3: 修改實作**

`backend/broker/capital_futures.py:280-289` 現在的 `_append_position_audit` 是：

```python
def _append_position_audit(*, raw: str, positions: list[dict[str, Any]]) -> None:
    try:
        path = _position_audit_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        summary = ";".join(
            f"{p.get('product')}/{p.get('direction_key')}x{p.get('qty')}" for p in positions
        )
        line = f"{ts}\tcount={len(positions)}\tpositions={summary}\traw={raw}\n"
        with path.open("a", encoding="utf-8") as f:
```

改成（加入 `account` 參數與欄位）：

```python
def _append_position_audit(*, account: str, raw: str, positions: list[dict[str, Any]]) -> None:
    try:
        path = _position_audit_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        summary = ";".join(
            f"{p.get('product')}/{p.get('direction_key')}x{p.get('qty')}" for p in positions
        )
        line = f"{ts}\taccount={account}\tcount={len(positions)}\tpositions={summary}\traw={raw}\n"
        with path.open("a", encoding="utf-8") as f:
```

再把呼叫端（約第 1427 行）：

```python
        _append_position_audit(raw=raw, positions=rows)
```

改成：

```python
        _append_position_audit(account=self.user_id or "", raw=raw, positions=rows)
```

- [ ] **Step 4: 執行確認通過**

Run: `pytest tests/test_capital_futures_broker.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/broker/capital_futures.py tests/test_capital_futures_broker.py
git commit -m "feat: tag position_audit.log lines with the account they belong to"
```

---

### Task 2：`user_broker_credentials` 資料表 + store 模組

**Files:**
- Create: `supabase/sql/2026-08-26_user_broker_credentials.sql`
- Create: `backend/broker_credentials_store.py`
- Create: `tests/test_broker_credentials_store.py`

**Interfaces:**
- Produces:
  - `get_broker_credentials(user_id: str, *, timeout: float = 10.0) -> dict | None`（回傳 `{"capital_user_id": str, "capital_password": str}` 或 `None`）
  - `admin_set_broker_credentials(user_id: str, *, capital_user_id: str, capital_password: str, timeout: float = 10.0) -> dict`

- [ ] **Step 1: 寫 SQL**

```sql
-- supabase/sql/2026-08-26_user_broker_credentials.sql
-- 每個登入帳號自己的群益期貨帳密（見
-- docs/superpowers/plans/2026-08-26-per-account-worker-process.md）。
-- 這張表存的是真實交易密碼，比 user_strategy_configs 敏感得多——一樣啟用
-- RLS、不建 policy，只有拿 SUPABASE_SECRET_KEY 的後端能讀寫；額外要求：
-- 絕對不能有任何前端網頁或一般 API 端點讀取這張表，只有 worker 行程啟動
-- 時用 SUPABASE_SECRET_KEY 直接讀。
create table if not exists public.user_broker_credentials (
  user_id uuid primary key references auth.users(id) on delete cascade,
  capital_user_id text not null,
  capital_password text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.user_broker_credentials enable row level security;
```

- [ ] **Step 2: 在 Supabase SQL Editor 執行**

貼上 Step 1 內容，Run，Table Editor 確認表存在、RLS 已啟用、沒有 policy。

- [ ] **Step 3: 寫失敗測試**

```python
# tests/test_broker_credentials_store.py
"""backend/broker_credentials_store.py 的單元測試：跟 test_strategy_config_store.py
用同一套 mock requests 的模式，不打真的 Supabase。這張表存真實交易密碼，
測試裡一律用假字串，絕對不要用真實帳密（就算是測試帳號也不要）。
"""

from unittest.mock import MagicMock, patch

import pytest

from backend.broker_credentials_store import (
    admin_set_broker_credentials,
    get_broker_credentials,
)


def test_get_returns_none_when_not_configured(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)

    assert get_broker_credentials("user-123") is None


def test_get_returns_none_when_no_row_exists(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")

    fake_response = MagicMock()
    fake_response.json.return_value = []
    fake_response.raise_for_status.return_value = None

    with patch("backend.broker_credentials_store.requests.get", return_value=fake_response):
        assert get_broker_credentials("user-123") is None


def test_get_returns_credentials_when_row_exists(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")

    fake_response = MagicMock()
    fake_response.json.return_value = [
        {"user_id": "user-123", "capital_user_id": "A123456789", "capital_password": "fake_pw"}
    ]
    fake_response.raise_for_status.return_value = None

    with patch("backend.broker_credentials_store.requests.get", return_value=fake_response) as mock_get:
        creds = get_broker_credentials("user-123")

    assert creds == {"capital_user_id": "A123456789", "capital_password": "fake_pw"}
    assert mock_get.call_args.kwargs["params"]["user_id"] == "eq.user-123"


def test_admin_set_raises_when_not_configured(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)

    with pytest.raises(RuntimeError):
        admin_set_broker_credentials(
            "user-123", capital_user_id="A123456789", capital_password="fake_pw",
        )


def test_admin_set_posts_correct_payload(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")

    fake_response = MagicMock()
    fake_response.json.return_value = [
        {"user_id": "user-123", "capital_user_id": "A123456789", "capital_password": "fake_pw"}
    ]
    fake_response.raise_for_status.return_value = None

    with patch("backend.broker_credentials_store.requests.post", return_value=fake_response) as mock_post:
        row = admin_set_broker_credentials(
            "user-123", capital_user_id="A123456789", capital_password="fake_pw",
        )

    assert row["capital_user_id"] == "A123456789"
    payload = mock_post.call_args.kwargs["json"]
    assert payload == {
        "user_id": "user-123",
        "capital_user_id": "A123456789",
        "capital_password": "fake_pw",
    }
    assert mock_post.call_args.kwargs["params"]["on_conflict"] == "user_id"
```

- [ ] **Step 4: 執行確認失敗**

Run: `pytest tests/test_broker_credentials_store.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 5: 寫實作**

```python
# backend/broker_credentials_store.py
"""backend/broker_credentials_store.py — 每個登入帳號自己的群益期貨帳密
（Supabase user_broker_credentials 表）。這張表存真實交易密碼，比
strategy_config_store.py 敏感得多；只有 backend/account_worker 啟動時
用 SUPABASE_SECRET_KEY 讀取，絕對不能透過任何一般 API 端點外流（見
docs/superpowers/plans/2026-08-26-per-account-worker-process.md）。
"""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)

TABLE = "user_broker_credentials"


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


def get_broker_credentials(user_id: str, *, timeout: float = 10.0) -> dict | None:
    """回傳這個帳號自己的群益帳密，沒設定過就回傳 None。安靜失敗（連線問題
    等）也回傳 None，讓呼叫端（worker 啟動流程）自己決定要不要中止——這裡
    不猜呼叫端想怎麼處理。"""
    if not _configured():
        return None
    try:
        resp = requests.get(
            f"{_base_url()}/rest/v1/{TABLE}",
            headers=_headers(),
            params={"user_id": f"eq.{user_id}", "select": "capital_user_id,capital_password"},
            timeout=timeout,
        )
        resp.raise_for_status()
        rows = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("讀取帳號 %s 的群益帳密失敗: %s", user_id, exc)
        return None
    if not rows:
        return None
    row = rows[0]
    return {"capital_user_id": row["capital_user_id"], "capital_password": row["capital_password"]}


def admin_set_broker_credentials(
    user_id: str, *, capital_user_id: str, capital_password: str, timeout: float = 10.0
) -> dict:
    """新增或覆蓋某個帳號的群益帳密。刻意沒有對應的一般 API 端點——只透過
    scripts/set_broker_credentials.py 這種本機工程操作呼叫，不透過網頁。"""
    if not _configured():
        raise RuntimeError("SUPABASE_URL/SUPABASE_SECRET_KEY 未設定，無法儲存帳密")

    payload = {
        "user_id": user_id,
        "capital_user_id": capital_user_id,
        "capital_password": capital_password,
    }
    headers = _headers()
    headers["Prefer"] = "resolution=merge-duplicates,return=representation"
    resp = requests.post(
        f"{_base_url()}/rest/v1/{TABLE}",
        headers=headers,
        params={"on_conflict": "user_id"},
        json=payload,
        timeout=timeout,
    )
    resp.raise_for_status()
    rows = resp.json()
    return rows[0] if rows else payload
```

- [ ] **Step 6: 執行確認通過**

Run: `pytest tests/test_broker_credentials_store.py -v`
Expected: 5 個測試全部 PASS

- [ ] **Step 7: Commit**

```bash
git add supabase/sql/2026-08-26_user_broker_credentials.sql backend/broker_credentials_store.py tests/test_broker_credentials_store.py
git commit -m "feat: add per-account broker credential store"
```

---

### Task 3：`scripts/set_broker_credentials.py`

**Files:**
- Create: `scripts/set_broker_credentials.py`

**Interfaces:**
- Consumes: `admin_set_broker_credentials(user_id, *, capital_user_id, capital_password)`（Task 2 產出）。

- [ ] **Step 1: 寫腳本**

```python
# -*- coding: utf-8 -*-
"""把某個登入帳號的群益期貨帳密存進資料庫，給該帳號自己的 worker 行程用
（見 docs/superpowers/plans/2026-08-26-per-account-worker-process.md）。

密碼一律用互動輸入（getpass），絕對不要當成命令列參數傳——命令列參數會
留在 shell history、`ps`/工作管理員的行程清單裡，等於把密碼寫在看得到
的地方。

用法（本專案根目錄執行，需要 .env 裡有 SUPABASE_URL/SUPABASE_SECRET_KEY）:
  python scripts/set_broker_credentials.py --user-id <supabase uuid> --capital-user-id A123456789
  （執行後會互動提示輸入群益交易密碼，不會顯示在螢幕上）
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

import truststore

truststore.inject_into_ssl()

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent.parent
load_dotenv(BASE / ".env")
sys.path.insert(0, str(BASE))

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

from backend.broker_credentials_store import admin_set_broker_credentials  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user-id", required=True, help="Supabase user id（uuid）")
    parser.add_argument("--capital-user-id", required=True, help="群益期貨帳號（身分證字號或期貨帳號）")
    args = parser.parse_args()

    password = getpass.getpass("群益交易密碼（輸入時不會顯示）：")
    if not password:
        print("密碼不能是空的", file=sys.stderr)
        return 1

    row = admin_set_broker_credentials(
        args.user_id, capital_user_id=args.capital_user_id, capital_password=password,
    )
    print(f"已儲存帳密：user_id={row['user_id']} capital_user_id={row['capital_user_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: 手動確認腳本能跑（不用真的存密碼也能測 argparse 部分）**

Run: `python scripts/set_broker_credentials.py --help`
Expected: 印出用法說明，不報錯。

- [ ] **Step 3: Commit**

```bash
git add scripts/set_broker_credentials.py
git commit -m "feat: add CLI script to set a per-account broker password via getpass"
```

---

### Task 4：`live_service.py` 支援 `ACCOUNT_USER_ID`/`WORKER_MODE`

**Files:**
- Modify: `backend/live_service.py`
- Create: `tests/test_live_service_worker_mode.py`

**Interfaces:**
- Produces:
  - `_is_worker_mode() -> bool`（讀 `WORKER_MODE` 環境變數）
  - `_apply_worker_account_credentials() -> None`（若 `ACCOUNT_USER_ID` 有設，讀該帳號的群益帳密覆蓋 `CAPITAL_USER_ID`/`CAPITAL_PASSWORD` 環境變數）

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_live_service_worker_mode.py
"""backend/live_service.py 的 worker 模式支援測試：ACCOUNT_USER_ID 設定時，
應該用該帳號自己的群益帳密覆蓋掉共用的 CAPITAL_USER_ID/CAPITAL_PASSWORD；
WORKER_MODE 設定時，poll_once() 的 K 線擁有邏輯（試讀群益歷史 K 線、
Shioaji 定期補缺）應該被跳過——K 線只由沒有設定 WORKER_MODE 的那個共用
行程負責讀寫。
"""

from unittest.mock import patch

from backend.live_service import _apply_worker_account_credentials, _is_worker_mode


def test_is_worker_mode_false_by_default(monkeypatch):
    monkeypatch.delenv("WORKER_MODE", raising=False)
    assert _is_worker_mode() is False


def test_is_worker_mode_true_when_set(monkeypatch):
    monkeypatch.setenv("WORKER_MODE", "1")
    assert _is_worker_mode() is True


def test_apply_worker_credentials_noop_without_account_user_id(monkeypatch):
    monkeypatch.delenv("ACCOUNT_USER_ID", raising=False)
    monkeypatch.setenv("CAPITAL_USER_ID", "original_user")

    _apply_worker_account_credentials()

    assert __import__("os").environ["CAPITAL_USER_ID"] == "original_user"


def test_apply_worker_credentials_overrides_when_account_user_id_set(monkeypatch):
    import os

    monkeypatch.setenv("ACCOUNT_USER_ID", "supabase-user-123")
    monkeypatch.setenv("CAPITAL_USER_ID", "shared_default_user")

    with patch(
        "backend.live_service.get_broker_credentials",
        return_value={"capital_user_id": "A_from_worker_account", "capital_password": "fake_pw"},
    ) as mock_get:
        _apply_worker_account_credentials()

    mock_get.assert_called_once_with("supabase-user-123")
    assert os.environ["CAPITAL_USER_ID"] == "A_from_worker_account"
    assert os.environ["CAPITAL_PASSWORD"] == "fake_pw"


def test_apply_worker_credentials_leaves_env_unchanged_when_account_not_found(monkeypatch):
    import os

    monkeypatch.setenv("ACCOUNT_USER_ID", "supabase-user-123")
    monkeypatch.setenv("CAPITAL_USER_ID", "original_user")

    with patch("backend.live_service.get_broker_credentials", return_value=None):
        _apply_worker_account_credentials()

    assert os.environ["CAPITAL_USER_ID"] == "original_user"
```

- [ ] **Step 2: 執行確認失敗**

Run: `pytest tests/test_live_service_worker_mode.py -v`
Expected: FAIL（`ImportError`）

- [ ] **Step 3: 加 import + 兩個函式**

在 `backend/live_service.py` 開頭的 import 區塊加入：

```python
from .broker_credentials_store import get_broker_credentials
```

在 `_load_project_env()`（約第 43-52 行）之後加入：

```python
def _is_worker_mode() -> bool:
    """WORKER_MODE=1 代表這個行程是某個帳號自己的 worker，不是共用的主行程
    ——差別見 docs/superpowers/plans/2026-08-26-per-account-worker-process.md。"""
    return os.getenv("WORKER_MODE", "").strip().lower() in {"1", "true", "yes"}


def _apply_worker_account_credentials() -> None:
    """ACCOUNT_USER_ID 有設的話，用那個帳號自己的群益帳密覆蓋
    CAPITAL_USER_ID/CAPITAL_PASSWORD；沒設、或那個帳號還沒被設定過帳密，
    就維持原本的值不變（安靜跳過，不中止開機——跟其他遠端密鑰的失敗處理
    方式一致）。"""
    account_user_id = os.getenv("ACCOUNT_USER_ID", "").strip()
    if not account_user_id:
        return
    creds = get_broker_credentials(account_user_id)
    if not creds:
        logger.warning("ACCOUNT_USER_ID=%s 還沒有設定群益帳密，維持原本的值", account_user_id)
        return
    os.environ["CAPITAL_USER_ID"] = creds["capital_user_id"]
    os.environ["CAPITAL_PASSWORD"] = creds["capital_password"]
```

- [ ] **Step 4: 執行確認通過**

Run: `pytest tests/test_live_service_worker_mode.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 把這兩個函式接進開機流程**

`backend/live_service.py` 的 `auto_connect_on_startup()`（約第 95-98 行）現在開頭是：

```python
def auto_connect_on_startup() -> None:
    """部署啟動時自動連線（讀 .env，不需手動按連線）。"""
    global _connect_error, current_product
    _load_project_env()
```

改成：

```python
def auto_connect_on_startup() -> None:
    """部署啟動時自動連線（讀 .env，不需手動按連線）。"""
    global _connect_error, current_product
    _load_project_env()
    _apply_worker_account_credentials()
```

再把 `poll_once()`（Task 之前已經改過的版本）裡，quote_ready 那個分支的開頭加上 worker 模式判斷。找到：

```python
    quote_product = resolve_quote_product_code(product)
    loaded = st.get("kline_loaded_products") or []
    if st.get("quote_ready") and quote_product not in loaded:
```

改成：

```python
    quote_product = resolve_quote_product_code(product)
    loaded = st.get("kline_loaded_products") or []
    if not _is_worker_mode() and st.get("quote_ready") and quote_product not in loaded:
```

同時把獨立定期跑的 Shioaji 補缺（Task 之前加的那段）也包進同樣的判斷。找到：

```python
    now = time.time()
    if _should_attempt_periodic_shioaji_backfill(now, _last_shioaji_backfill_attempt):
```

改成：

```python
    now = time.time()
    if not _is_worker_mode() and _should_attempt_periodic_shioaji_backfill(now, _last_shioaji_backfill_attempt):
```

- [ ] **Step 6: 執行確認通過（含既有測試沒壞掉）**

Run: `pytest tests/ -v`
Expected: 全部 PASS

- [ ] **Step 7: Commit**

```bash
git add backend/live_service.py tests/test_live_service_worker_mode.py
git commit -m "feat: support per-account credentials and K-line-skip in worker mode"
```

---

### Task 5：Worker 啟動時自動 arm 已綁定且啟用的策略

**Files:**
- Modify: `backend/api.py`（`lifespan`）
- Create: `tests/test_api_worker_auto_arm.py`

**Interfaces:**
- Consumes: `list_user_strategy_configs(user_id)`（既有）、`start_strategy(strategy_id, product_code, qty)`（既有）、`_is_worker_mode()`（Task 4 產出）。
- Produces: `_auto_arm_bound_strategies() -> None`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_api_worker_auto_arm.py
"""worker 行程啟動時，應該把 ACCOUNT_USER_ID 這個帳號已經綁定且 enabled=true
的策略自動 arm 起來——這是 worker 專屬行為，不是使用者在網頁上按開關（那個
機制已經在 2026-08-26 的帳號↔策略配對計畫裡拿掉了，見那份計畫文件）。"""

from unittest.mock import patch

from backend.api import _auto_arm_bound_strategies


def test_does_nothing_when_not_worker_mode(monkeypatch):
    monkeypatch.delenv("WORKER_MODE", raising=False)

    with patch("backend.api.start_strategy") as mock_start:
        _auto_arm_bound_strategies()

    mock_start.assert_not_called()


def test_does_nothing_when_account_user_id_unset(monkeypatch):
    monkeypatch.setenv("WORKER_MODE", "1")
    monkeypatch.delenv("ACCOUNT_USER_ID", raising=False)

    with patch("backend.api.start_strategy") as mock_start:
        _auto_arm_bound_strategies()

    mock_start.assert_not_called()


def test_arms_only_enabled_bound_strategies(monkeypatch):
    monkeypatch.setenv("WORKER_MODE", "1")
    monkeypatch.setenv("ACCOUNT_USER_ID", "user-123")

    rows = [
        {"strategy_id": "breakout_long", "product_code": "TM2609", "qty": 2, "enabled": True},
        {"strategy_id": "breakout_short", "product_code": "TM2609", "qty": 1, "enabled": False},
    ]
    with patch("backend.api.list_user_strategy_configs", return_value=rows) as mock_list, \
         patch("backend.api.start_strategy") as mock_start:
        _auto_arm_bound_strategies()

    mock_list.assert_called_once_with("user-123")
    mock_start.assert_called_once_with("breakout_long", "TM2609", qty=2)
```

- [ ] **Step 2: 執行確認失敗**

Run: `pytest tests/test_api_worker_auto_arm.py -v`
Expected: FAIL（`ImportError`）

- [ ] **Step 3: 加函式 + 接進 lifespan**

在 `backend/api.py` 的 `strategy_loop()`（約第 86-93 行）之前加入：

```python
def _auto_arm_bound_strategies() -> None:
    """worker 行程專屬：啟動時把這個帳號已經綁定且 enabled=true 的策略自動
    arm 起來。一般共用行程（沒設 WORKER_MODE）完全不做這件事——現有單一
    共用帳號的行為不受影響。"""
    from .live_service import _is_worker_mode

    if not _is_worker_mode():
        return
    account_user_id = os.getenv("ACCOUNT_USER_ID", "").strip()
    if not account_user_id:
        return
    for row in list_user_strategy_configs(account_user_id):
        if not row.get("enabled"):
            continue
        start_strategy(row["strategy_id"], row["product_code"], qty=row.get("qty"))
```

再修改 `lifespan`（約第 96-108 行），現在是：

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    """部署啟動時於背景自動連線群益 API，不阻塞 uvicorn 啟動與網頁服務。"""
    asyncio.create_task(asyncio.to_thread(auto_connect_on_startup))
    poll_task = asyncio.create_task(poll_loop())
    strat_task = asyncio.create_task(strategy_loop())
    yield
```

改成：

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    """部署啟動時於背景自動連線群益 API，不阻塞 uvicorn 啟動與網頁服務。"""
    asyncio.create_task(asyncio.to_thread(auto_connect_on_startup))
    asyncio.create_task(asyncio.to_thread(_auto_arm_bound_strategies))
    poll_task = asyncio.create_task(poll_loop())
    strat_task = asyncio.create_task(strategy_loop())
    yield
```

- [ ] **Step 4: 執行確認通過**

Run: `pytest tests/ -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/api.py tests/test_api_worker_auto_arm.py
git commit -m "feat: auto-arm bound+enabled strategies on worker process startup"
```

---

### Task 6：手動雙開驗證步驟（文件，不是自動化測試）

**Files:**
- Create: `docs/worker_process_manual_test.md`

- [ ] **Step 1: 寫文件**

```markdown
# 手動驗證：兩個帳號的 worker 行程同時運行

前提：已經有一個真實的第二個群益期貨帳號，本人已完成 CA 憑證申請 +
SKCOMVerifyDJ 連線測試（跟你自己那次一樣的流程），且已經在這台機器上
用 Windows 憑證匯入精靈把他的憑證也裝進「目前使用者」憑證存放區。

## Step 1：存這個帳號的密碼

```
python scripts/set_broker_credentials.py --user-id <他的 Supabase user id> --capital-user-id <他的群益帳號>
```
（會提示輸入密碼，交給他自己在鍵盤上輸入，不要你自己打。）

## Step 2：幫他綁定至少一個策略（沿用既有的 bind_strategy.py）

```
python scripts/bind_strategy.py --user-id <他的 user id> --strategy-id breakout_long --product-code TM2609 --qty 1 --enabled
```

## Step 3：啟動你自己的共用主行程（跟平常一樣，port 8765）

```
python run_server.py
```

## Step 4：另開一個終端機，啟動他的 worker 行程（不同 port，設 WORKER_MODE + ACCOUNT_USER_ID）

Windows PowerShell：
```
$env:WORKER_MODE="1"
$env:ACCOUNT_USER_ID="<他的 Supabase user id>"
$env:TMF_PORT="8766"
python run_server.py
```

## Step 5：確認兩邊都連線成功

- 主行程（8765）：`http://127.0.0.1:8765/api/health`
- 他的 worker（8766）：`http://127.0.0.1:8766/api/health`

兩邊都要回 `{"status":"ok"}`。接著分別檢查兩邊的 log（沒有 uvicorn console 輸出可看的話，检查
`data/order_audit.log`/`data/position_audit.log` 有沒有出現兩種不同的 `account=`
標記——這是驗證「兩個帳號真的各自連線成功、各自在跑」最直接的證據，
比看 console log 可靠（見這次 session 之前發現的 logging 設定問題）。

## 這一步要看的是什麼

- 兩邊都connect成功、都看得到各自的持倉/委託 → 並發連線可行，Plan B-2
  （跨帳號請求路由）可以開始規劃。
- 只有一邊連得上，另一邊報錯或卡住 → 記下實際的錯誤訊息/行為，回來討論
  是不是要走「兩台機器」而不是「一台機器兩個行程」。
```

- [ ] **Step 2: Commit**

```bash
git add docs/worker_process_manual_test.md
git commit -m "docs: add manual dual-account worker verification steps"
```

---

## Self-Review Notes

- **Spec coverage**：「你开始规划然后实作」——Task 1-5 是可以現在做、不需要真實第二帳號就能完整測試的部分（帳密儲存、worker 模式判斷、自動 arm）；Task 6 是需要真實第二帳號才能執行的手動驗證步驟，寫成文件而非自動化測試。跨帳號的 API/WebSocket 請求路由（讓使用者登入後自動連到自己 worker 的機制）刻意不在這個計畫裡——那是 Plan B-2，等 Task 6 驗證過並發連線真的可行之後才值得做。
- **關鍵技術決策**：一帳號一行程（不是一行程多執行緒），理由是 STA COM 物件跨執行緒呼叫的風險，寫在計畫最前面的 Architecture 段落。
- **Placeholder scan**：所有 code block 都是完整可執行內容。
- **Type consistency**：`get_broker_credentials(user_id) -> dict | None`、`admin_set_broker_credentials(user_id, *, capital_user_id, capital_password) -> dict` 在 Task 2 定義，Task 4 的 mock 呼叫方式一致。
