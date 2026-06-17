# -*- mode: python ; coding: utf-8 -*-
# Сборка одного .exe под Windows:  pyinstaller build.spec
# (PyInstaller не кросс-компилит — запускать на Windows / windows-runner.)

from PyInstaller.utils.hooks import collect_all

block_cipher = None

# pywebview тащит за собой бэкенд (на Windows — EdgeChromium/WebView2 через
# pythonnet) и свои ресурсы — собираем их явно, иначе окно не поднимется.
ws_datas, ws_binaries, ws_hidden = collect_all("webview")

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=ws_binaries,
    # шаблон .docx кладём в корень бандла -> paths.resource_path("...") его найдёт
    datas=[("Шаблон_вкладыш_ЧИСТЫЙ.docx", ".")] + ws_datas,
    hiddenimports=ws_hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="Vkladyshi",   # ASCII: GitHub Release не принимает не-ASCII имена ассетов
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    runtime_tmpdir=None,
    console=False,          # GUI-приложение, без чёрного окна консоли
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon="app.ico",       # подключим, когда будет иконка
)
