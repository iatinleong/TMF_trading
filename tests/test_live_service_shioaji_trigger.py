"""backend/live_service.py 的 Shioaji 定期補缺觸發邏輯測試。

2026-08-26 實測抓到的問題：poll_once() 原本把 Shioaji 補缺包在「群益自己的
quote_ready 變 true」這個分支底下——但群益的 OnConnection(3003) 事件實測過
會間歇性完全不觸發（見 docs 的舊調查紀錄），quote_ready 卡住不變 true，
Shioaji 這個本來要拿來自動補救的安全網就永遠沒機會執行。Shioaji 是完全獨立
的第三方連線，不需要群益報價「就緒」才能查，這裡測的是把觸發邏輯改成定期
（跟 quote_ready 脫鉤）之後的判斷函式，不測整個 poll_once()（那個依賴真實
broker 單例，不好在單元測試裡建置）。
"""

from backend.live_service import (
    SHIOAJI_BACKFILL_INTERVAL_SECONDS,
    _should_attempt_periodic_shioaji_backfill,
)


def test_attempts_on_first_call_when_never_attempted_before():
    # last_attempt=0.0 代表程式剛啟動、還沒試過——第一次應該要試。
    assert _should_attempt_periodic_shioaji_backfill(now=1000.0, last_attempt=0.0) is True


def test_does_not_attempt_again_within_interval():
    now = 1000.0
    last_attempt = now - (SHIOAJI_BACKFILL_INTERVAL_SECONDS - 1)
    assert _should_attempt_periodic_shioaji_backfill(now=now, last_attempt=last_attempt) is False


def test_attempts_again_once_interval_has_passed():
    now = 1000.0
    last_attempt = now - (SHIOAJI_BACKFILL_INTERVAL_SECONDS + 1)
    assert _should_attempt_periodic_shioaji_backfill(now=now, last_attempt=last_attempt) is True


def test_interval_is_reasonable_and_not_too_aggressive():
    # 太短會浪費 Shioaji API 額度（find_missing_bars 雖然便宜，但還是要打
    # ticks() 查詢才知道有沒有缺），太長又會讓缺口拖很久才自動補上。
    assert 60.0 <= SHIOAJI_BACKFILL_INTERVAL_SECONDS <= 900.0
