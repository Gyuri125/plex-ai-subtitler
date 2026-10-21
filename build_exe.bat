@echo off
chcp 65001 >nul
title Plex AI Subtitler - Build & Auto-Updater

echo =============================================================================
echo   🎬 Plex AI Subtitler - Automata Fordító és Frissítő Rendszer
echo =============================================================================
echo.

:: 1. LÉPÉS: Git frissítések lekérdezése a GitHubról
echo [1/5] Frissítések ellenőrzése a GitHub tárhelyről (git pull)...
where git >nul 2>&1
if %ERRORLEVEL% equ 0 (
    git pull origin main
    if %ERRORLEVEL% equ 0 (
        echo [OK] Legfrissebb forráskód letöltve / naprakész!
    ) else (
        echo [FIGYELEM] Nem sikerült a git pull, a helyi kódbázissal folytatjuk.
    )
) else (
    echo [FIGYELEM] A Git parancs nem található, a helyi kóddal folytatjuk.
)
echo.

:: 2. LÉPÉS: Python környezet megkeresése
echo [2/5] Python környezet azonosítása...
set PYTHON_CMD=python
if exist ".venv\Scripts\python.exe" (
    set PYTHON_CMD=.venv\Scripts\python.exe
    echo [OK] Helyi virtuális környezet (.venv) használata.
) else (
    echo [OK] Rendszerszintű Python használata.
)
echo.

:: 3. LÉPÉS: Függőségek és PyInstaller frissítése
echo [3/5] Függőségek és PyInstaller ellenőrzése...
%PYTHON_CMD% -m pip install -r requirements.txt pyinstaller --quiet
if %ERRORLEVEL% neq 0 (
    echo [HIBA] Nem sikerült a csomagok telepítése!
    pause
    exit /b %ERRORLEVEL%
)
echo [OK] Minden csomag naprakész!
echo.

:: 4. LÉPÉS: Futó folyamat bezárása és korábbi átmeneti fájlok törlése
echo [4/5] Régi futó folyamatok leállítása és átmeneti fájlok takarítása...
taskkill /F /IM PlexSubtitler.exe >nul 2>&1
if exist "build" rd /s /q "build" 2>nul
echo [OK] Építési könyvtár tiszta.
echo.

:: 5. LÉPÉS: PyInstaller fordítás indítása
echo [5/5] PlexSubtitler.exe fordítása folyamatban (PyInstaller)...
echo Ez a folyamat a hardvertől függően 1-2 percet vehet igénybe...
%PYTHON_CMD% -m PyInstaller PlexSubtitler.spec --noconfirm

if exist "dist\PlexSubtitler\PlexSubtitler.exe" (
    echo.
    echo =============================================================================
    echo   🎉 SIKERES FORDÍTÁS! AZ ÚJ VERZIÓ ELKÉSZÜLT ÉS FELÜLÍRTA A RÉGIT!
    echo =============================================================================
    echo   Futtatható fájl helye:
    echo   dist\PlexSubtitler\PlexSubtitler.exe
    echo =============================================================================
    echo.
    set /p INDITAS="Szeretnéd most elindítani a frissen fordított programot? (I/N): "
    if /i "%INDITAS%"=="I" (
        start "" "dist\PlexSubtitler\PlexSubtitler.exe"
    )
) else (
    echo.
    echo =============================================================================
    echo   ❌ HIBA TÖRTÉNT A FORDÍTÁS SORÁN!
    echo   Ellenőrizd a fenti hibaüzeneteket.
    echo =============================================================================
)

echo.
echo Nyomj meg egy billentyűt a kilépéshez...
pause >nul
