"""backend/load_taifex_tick.py::ensure_tmfr1_data_available() 的自動補檔測試。

背景：data/raw_tick/TMFR1（252MB/243 個 parquet 檔）每次都要手動搬到新 VM 很麻煩，
改成啟動/回測前自動檢查、缺檔就從 Supabase Storage 補回（沿用專案既有的
SUPABASE_URL + SUPABASE_SECRET_KEY，見 backend/secrets_store.py，不用另外管理
一組雲端憑證）。本機/VM 只要 .env 沒設定這兩個值，就應該直接跳過、不影響既有行為。
"""

from unittest.mock import MagicMock, patch

import pytest
import requests

from backend.load_taifex_tick import ensure_tmfr1_data_available


def _mock_response(*, json_data=None, content: bytes = b"", raise_for_status_error=None):
    resp = MagicMock()
    if raise_for_status_error:
        resp.raise_for_status.side_effect = raise_for_status_error
    else:
        resp.raise_for_status.return_value = None
    if json_data is not None:
        resp.json.return_value = json_data
    resp.iter_content.return_value = [content] if content else []
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


@pytest.fixture(autouse=True)
def _supabase_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "test-service-key")


def test_skips_silently_when_supabase_not_configured(tmp_path, monkeypatch):
    """本機/VM 沒設定 SUPABASE_URL/SUPABASE_SECRET_KEY 時，什麼都不做，也不拋錯。"""
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
    tmfr1_dir = tmp_path / "TMFR1"

    with patch("backend.load_taifex_tick.requests.post") as mock_post:
        ensure_tmfr1_data_available(tmfr1_dir)

    mock_post.assert_not_called()
    assert not tmfr1_dir.exists()


def test_skips_silently_when_list_request_fails(tmp_path):
    """設定齊全，但列出檔案清單這一步失敗（例如 bucket 不存在/金鑰錯）時，只記警告不拋錯。"""
    tmfr1_dir = tmp_path / "TMFR1"
    list_resp = _mock_response(raise_for_status_error=requests.HTTPError("404"))

    with patch("backend.load_taifex_tick.requests.post", return_value=list_resp):
        ensure_tmfr1_data_available(tmfr1_dir)

    assert not tmfr1_dir.exists()


def test_no_download_when_all_files_already_present(tmp_path):
    """本機已經有 Supabase 上列出的全部檔案時，不應該再發任何下載請求。"""
    tmfr1_dir = tmp_path / "TMFR1"
    tmfr1_dir.mkdir()
    (tmfr1_dir / "TMFR1_2024-07-29.parquet").write_bytes(b"existing")

    list_resp = _mock_response(json_data=[{"name": "TMFR1_2024-07-29.parquet"}])

    with patch("backend.load_taifex_tick.requests.post", return_value=list_resp), \
         patch("backend.load_taifex_tick.requests.get") as mock_get:
        ensure_tmfr1_data_available(tmfr1_dir)

    mock_get.assert_not_called()


def test_downloads_only_missing_files(tmp_path):
    """本機缺檔時，只補下載缺的那幾個，已存在的檔案不動。"""
    tmfr1_dir = tmp_path / "TMFR1"
    tmfr1_dir.mkdir()
    (tmfr1_dir / "TMFR1_2024-07-29.parquet").write_bytes(b"already-have-this")

    list_resp = _mock_response(
        json_data=[
            {"name": "TMFR1_2024-07-29.parquet"},
            {"name": "TMFR1_2024-07-30.parquet"},
        ]
    )
    download_resp = _mock_response(content=b"downloaded-bytes")

    with patch("backend.load_taifex_tick.requests.post", return_value=list_resp), \
         patch("backend.load_taifex_tick.requests.get", return_value=download_resp) as mock_get:
        ensure_tmfr1_data_available(tmfr1_dir)

    assert mock_get.call_count == 1
    downloaded = tmfr1_dir / "TMFR1_2024-07-30.parquet"
    assert downloaded.exists()
    assert downloaded.read_bytes() == b"downloaded-bytes"
    # 原本就存在的檔案內容不應該被覆蓋
    assert (tmfr1_dir / "TMFR1_2024-07-29.parquet").read_bytes() == b"already-have-this"


def test_survives_individual_file_download_failure(tmp_path):
    """單一檔案下載失敗時，不應該讓整個補檔流程中斷或留下半殘檔。"""
    tmfr1_dir = tmp_path / "TMFR1"

    list_resp = _mock_response(json_data=[{"name": "TMFR1_2024-07-29.parquet"}])

    with patch("backend.load_taifex_tick.requests.post", return_value=list_resp), \
         patch("backend.load_taifex_tick.requests.get", side_effect=requests.Timeout("slow")):
        ensure_tmfr1_data_available(tmfr1_dir)  # 不應拋出例外

    assert not (tmfr1_dir / "TMFR1_2024-07-29.parquet").exists()
    assert not (tmfr1_dir / "TMFR1_2024-07-29.parquet.part").exists()


def test_ignores_non_parquet_entries_in_listing(tmp_path):
    """Supabase 資料夾清單裡如果混了非 parquet 項目（例如子資料夾標記），應該被忽略。"""
    tmfr1_dir = tmp_path / "TMFR1"

    list_resp = _mock_response(
        json_data=[
            {"name": "TMFR1_2024-07-29.parquet"},
            {"name": ".emptyFolderPlaceholder"},
        ]
    )
    download_resp = _mock_response(content=b"data")

    with patch("backend.load_taifex_tick.requests.post", return_value=list_resp), \
         patch("backend.load_taifex_tick.requests.get", return_value=download_resp) as mock_get:
        ensure_tmfr1_data_available(tmfr1_dir)

    assert mock_get.call_count == 1
    assert (tmfr1_dir / "TMFR1_2024-07-29.parquet").exists()
    assert not (tmfr1_dir / ".emptyFolderPlaceholder").exists()
