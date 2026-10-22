# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：生成单文件 exe（含 playwright 驱动、配置模板、tkinter/opencv）

打包命令：
    .venv\\Scripts\\pyinstaller.exe --clean --noconfirm 签到助手.spec

产物：dist/签到助手.exe（单文件，双击启动图形界面；首次运行会在同目录释放 config/）
"""
import os

import playwright as _pw
from PyInstaller.utils.hooks import collect_dynamic_libs

PW_DIR = os.path.dirname(_pw.__file__)

datas = [
    # playwright 的 node 驱动（程序用系统 Edge，无需下载浏览器）
    (os.path.join(PW_DIR, "driver"), "playwright/driver"),
    # 初始配置模板（首次运行释放到 exe 同目录，用户可编辑）
    ("config", "config"),
]
# pyzbar 依赖的 libzbar/libiconv DLL
datas += collect_dynamic_libs("pyzbar")

hiddenimports = [
    "core.browser", "core.ai", "core.captcha", "core.slider", "core.cf",
    "core.cookiecloud", "core.totp", "core.twofa_store", "core.events",
    "core.config", "core.logger", "core.state", "core.scheduler", "core.captcha",
    "tasks.base", "tasks.manager", "tasks.nexusphp",
    "tasks.pt_kong", "tasks.hddolby", "tasks.ttg", "tasks.mteam", "tasks.audiences",
    "tasks.akile", "tasks.hhan",
    "ui.gui",
    "cryptography.hazmat.primitives.ciphers",
    "cryptography.hazmat.primitives.padding",
    "pyzbar.pyzbar", "pyzbar.wrapper",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib", "pandas", "scipy", "PyQt5", "PySide2", "PySide6",
        "IPython", "pytest", "notebook", "jupyter", "setuptools",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="签到助手",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # 关闭 UPX，避免杀软误报
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,       # 保留控制台便于查看运行日志（双击时同时出现 GUI）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
