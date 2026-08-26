# -*- mode: python ; coding: utf-8 -*-

# 2026-08-25：shioaji（backend/shioaji_backfill.py 用，群益 K 線回補缺口的
# 備援資料源）用 _core.pyd 這個編譯過的核心模組動態載入其餘子模組，
# PyInstaller 靜態分析抓不到完整依賴——實測過，沒加 collect_all 的話，
# 凍結後的 exe 裡 shioaji 資料夾只有 _core.pyd 一個檔案，其餘 account.py/
# contracts.py/order.py 等全部漏掉，import shioaji 會直接失敗。
from PyInstaller.utils.hooks import collect_all

shioaji_datas, shioaji_binaries, shioaji_hiddenimports = collect_all('shioaji')

a = Analysis(
    ['run_server.py'],
    pathex=[],
    binaries=shioaji_binaries,
    datas=shioaji_datas,
    hiddenimports=['pythoncom', 'pywintypes'] + shioaji_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='tmf-backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='tmf-backend',
)
