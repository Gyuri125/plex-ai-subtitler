# -*- mode: python ; coding: utf-8 -*-
import os

datas = []
if os.path.exists('plex_subtitler/settings.json'):
    datas.append(('plex_subtitler/settings.json', '.'))
elif os.path.exists('plex_subtitler/settings.json.example'):
    datas.append(('plex_subtitler/settings.json.example', '.'))

a = Analysis(
    ['plex_subtitler\\app1_extractor\\main.py'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=[
        'plex_subtitler',
        'plex_subtitler.notifier',
        'plex_subtitler.config',
        'plex_subtitler.logger',
        'plex_subtitler.app1_extractor.subtitle_extractor',
        'plex_subtitler.app1_extractor.hardware_manager',
        'plex_subtitler.app2_translator.translator_daemon',
        'plex_subtitler.app2_translator.llm_client',
        'plex_subtitler.app2_translator.srt_parser',
        'pystray',
        'PIL',
        'customtkinter',
        'imageio_ffmpeg',
    ],
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
    name='PlexSubtitler',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
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
    name='PlexSubtitler',
)
