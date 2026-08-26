"""Entry point for the packaged (PyInstaller) desktop build.

Equivalent to `uvicorn backend.api:app --host 0.0.0.0 --port 8765`, but
imports the app object directly instead of a dynamic "module:attr" string so
it resolves correctly inside a frozen bundle.
"""
from __future__ import annotations

import os

# 2026-08-21 實測發現：這台機器（以及可能其他使用者的電腦，例如公司防毒/防火牆
# 會攔截 HTTPS 流量重新簽章）用 Python 內建的 certifi 憑證清單驗證會失敗
# （連 google.com/github.com 都一樣，不是 Supabase 特有的問題），但瀏覽器本身
# 連得通——因為瀏覽器信任 Windows 系統的憑證存放區，certifi 的清單沒有。
# truststore 讓 Python 的 ssl 改用 Windows 系統信任的憑證，必須在任何模組建立
# SSL 連線之前（包括 backend.api 內部會用到 requests/PyJWKClient 的地方）就
# 注入，所以放在所有其他 import 之前。
import truststore

truststore.inject_into_ssl()

import uvicorn

from backend.api import app

if __name__ == "__main__":
    port = int(os.environ.get("TMF_PORT", "8765"))
    uvicorn.run(app, host="0.0.0.0", port=port)
