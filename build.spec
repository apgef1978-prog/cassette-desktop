# -*- mode: python ; coding: utf-8 -*-
# Сборка одного .exe под Windows:  pyinstaller build.spec
# (PyInstaller не кросс-компилит — запускать на Windows / windows-runner.)

block_cipher = None

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    # шаблон .docx кладём в корень бандла -> paths.resource_path("...") его найдёт
    datas=[("Шаблон_вкладыш_ЧИСТЫЙ.docx", ".")],
    hiddenimports=[],
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
    name="Вкладыши",
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
