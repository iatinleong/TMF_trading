# -*- coding: utf-8 -*-
"""印出 OnShowAgreement 原始字串（repr + 多種解碼嘗試），找出未簽署的同意書。"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

base_dir = Path(__file__).resolve().parent.parent
load_dotenv(base_dir / ".env")
sys.path.insert(0, str(base_dir))

import comtypes.client  # noqa: E402

DLL = base_dir / (
    "CapitalAPI_2.13.58_PythonExample/CapitalAPI_2.13.58_PythonExample/元件/x64/SKCOM.dll"
)

rows: list[str] = []


def try_decodes(s: str) -> list[str]:
    outs = []
    outs.append(("as-is", s))
    try:
        outs.append(("latin1->cp950", s.encode("latin1", "replace").decode("cp950", "replace")))
    except Exception:
        pass
    try:
        outs.append(("cp950-bytes->utf8", s.encode("cp950", "replace").decode("utf-8", "replace")))
    except Exception:
        pass
    try:
        # BSTR 每個 wchar 只放一個 ANSI byte 的情況
        outs.append(("lowbyte->cp950", bytes(ord(c) & 0xFF for c in s).decode("cp950", "replace")))
    except Exception:
        pass
    return outs


def main() -> None:
    os.add_dll_directory(str(DLL.parent))
    comtypes.client.GetModule(str(DLL))
    import comtypes.gen.SKCOMLib as sk

    center = comtypes.client.CreateObject(sk.SKCenterLib, interface=sk.ISKCenterLib)
    reply = comtypes.client.CreateObject(sk.SKReplyLib, interface=sk.ISKReplyLib)

    class ReplyEvents:
        def OnReplyMessage(self, bstrUserID, bstrMessages):  # noqa: N802
            return -1

    class CenterEvents:
        def OnShowAgreement(self, bstrData):  # noqa: N802
            rows.append(bstrData)

    _r = comtypes.client.GetEvents(reply, ReplyEvents())
    _c = comtypes.client.GetEvents(center, CenterEvents())

    user_id = os.getenv("CAPITAL_USER_ID")
    password = os.getenv("CAPITAL_PASSWORD")
    center.SKCenterLib_SetAuthority(int(os.getenv("CAPITAL_ENVIRONMENT", "0")))
    code = center.SKCenterLib_Login(user_id, password)
    print("login code =", code, center.SKCenterLib_GetReturnCodeMessage(code))

    code = center.SKCenterLib_RequestAgreement(user_id)
    print("RequestAgreement code =", code)
    comtypes.client.PumpEvents(3.0)

    print(f"\n=== 共 {len(rows)} 筆 ===")
    for i, raw in enumerate(rows):
        print(f"\n--- row {i} ---")
        print("repr:", repr(raw))
        for name, val in try_decodes(raw):
            print(f"  [{name}] {val}")


if __name__ == "__main__":
    main()
