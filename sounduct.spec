# -*- mode: python ; coding: utf-8 -*-
# sounduct.spec — PyInstaller config para Sounduct v1
#
# Uso:
#   pip install pyinstaller
#   pyinstaller sounduct.spec
#
# El ejecutable queda en dist/Sounduct.exe

import os

block_cipher = None

# Archivos de datos a incluir junto al ejecutable
datas = [
    ('popup_carpeta.html',  '.'),
    ('popup_settings.html', '.'),
    ('popup_sync.html',     '.'),
    ('styles.css',          '.'),
]

# Incluir sounduct.ico si existe
if os.path.exists('sounduct.ico'):
    datas.append(('sounduct.ico', '.'))

a = Analysis(
    ['housero.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[
        'watchdog.observers.polling',
        'PyQt6.QtWebEngineWidgets',
        'PyQt6.QtWebChannel',
        'PyQt6.QtWebEngineCore',
    ],
    hookspath=[],
    hooksconfig={},
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
    name='Sounduct',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # S20: sin terminal visible
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='sounduct.ico',    # S27: icono propio
)
