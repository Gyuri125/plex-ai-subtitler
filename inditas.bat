@echo off
chcp 65001 >nul
title Plex AI Subtitler
echo Indítás folyamatban...

set PYTHON_CMD=python
if exist ".venv\Scripts\python.exe" (
    .venv\Scripts\python.exe --version >nul 2>&1
    if %ERRORLEVEL% equ 0 (
        set PYTHON_CMD=.venv\Scripts\python.exe
    )
)

start "" %PYTHON_CMD% -m plex_subtitler.app1_extractor.main
