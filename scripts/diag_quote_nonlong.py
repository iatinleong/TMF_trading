# -*- coding: utf-8 -*-
"""對照實驗：改用舊式 SKQuoteLib_EnterMonitor（非 LONG）測試 OnConnection 是否觸發。"""
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

base_dir = Path(__file__).resolve().parent.parent
load_dotenv(base_dir / ".env")
sys.path.insert(0, str(base_dir))

import comtypes.client  # noqa: E402

DLL = base_dir / (
    "CapitalAPI_2.13.58_PythonExample/CapitalAPI_2.13.58_PythonExample/元件/x64/SKCOM.dll"
)
WATCH_SECONDS = 150


def main() -> None:
    os.add_dll_directory(str(DLL.parent))
    comtypes.client.GetModule(str(DLL))
    import comtypes.gen.SKCOMLib as sk

    center = comtypes.client.CreateObject(sk.SKCenterLib, interface=sk.ISKCenterLib)
    reply = comtypes.client.CreateObject(sk.SKReplyLib, interface=sk.ISKReplyLib)
    quote = comtypes.client.CreateObject(sk.SKQuoteLib, interface=sk.ISKQuoteLib)

    events: list[str] = []

    class ReplyEvents:
        def OnReplyMessage(self, bstrUserID, bstrMessages):  # noqa: N802
            return -1

    class QuoteEvents:
        def OnConnection(self, nKind, nCode):  # noqa: N802
            msg = f"OnConnection kind={nKind} code={nCode}"
            events.append(msg)
            print(f"  >>> {msg}")

    _r = comtypes.client.GetEvents(reply, ReplyEvents())
    _q = comtypes.client.GetEvents(quote, QuoteEvents())

    user_id = os.getenv("CAPITAL_USER_ID")
    password = os.getenv("CAPITAL_PASSWORD")
    center.SKCenterLib_SetAuthority(int(os.getenv("CAPITAL_ENVIRONMENT", "0")))
    code = center.SKCenterLib_Login(user_id, password)
    print("login:", code, center.SKCenterLib_GetReturnCodeMessage(code))
    code = reply.SKReplyLib_ConnectByID(user_id)
    print("ConnectByID:", code, center.SKCenterLib_GetReturnCodeMessage(code))
    comtypes.client.PumpEvents(2.0)

    code = quote.SKQuoteLib_EnterMonitor()
    print("EnterMonitor(非LONG):", code, center.SKCenterLib_GetReturnCodeMessage(code))

    start = time.time()
    last = None
    while time.time() - start < WATCH_SECONDS:
        comtypes.client.PumpEvents(1.0)
        status = quote.SKQuoteLib_IsConnected()
        elapsed = int(time.time() - start)
        if status != last:
            print(f"  [{elapsed:3d}s] IsConnected={status}")
            last = status
        if any("kind=3003" in e for e in events):
            print(f"  [{elapsed:3d}s] >>> 3003 商品檔下載完成（非LONG 通道正常）")
            break

    print("=== 結束 ===")
    print("收到的 OnConnection 事件：", events or "（一個都沒有）")


if __name__ == "__main__":
    main()
