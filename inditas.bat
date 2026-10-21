@echo off
chcp 65001 >nul
title Plex AI Subtitler
echo Indítás folyamatban...
if exist ".venv\Scripts\python.exe" (
    start "" .venv\Scripts\pythonw.exe -m plex_subtitler.app1_extractor.main
) else (
    start "" pythonw -m plex_subtitler.app1_extractor.main
)
