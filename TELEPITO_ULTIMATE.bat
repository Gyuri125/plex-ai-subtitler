@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul

title Plex Subtitler v1.3.0 - Ultimate Telepito (Hardverfelmeressel)

set "SRC_DIR=%~dp0"
if "%SRC_DIR:~-1%"=="\" set "SRC_DIR=%SRC_DIR:~0,-1%"
set "LOG_DIR=%SRC_DIR%\logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>&1

for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss" 2^>nul') do set "STAMP=%%I"
if "%STAMP%"=="" set "STAMP=manual"
set "BAT_LOG=%LOG_DIR%\install_bat_%STAMP%.log"

echo ====================================================================
echo   PLEX SUBTITLER v1.3.0 - ULTIMATE TELEPITO (.BAT INDITO)
echo ====================================================================
echo Inditas ideje: %DATE% %TIME% > "%BAT_LOG%"
echo Gepnev: %COMPUTERNAME% ^| Felhasznalo: %USERNAME% >> "%BAT_LOG%"
echo Forras mappa: %SRC_DIR% >> "%BAT_LOG%"
echo Parameterek: %* >> "%BAT_LOG%"

if not exist "%SRC_DIR%\setup_ultimate.ps1" (
    echo [HIBA] Nem talalhato a setup_ultimate.ps1: "%SRC_DIR%"
    echo [HIBA] Nem talalhato a setup_ultimate.ps1: "%SRC_DIR%" >> "%BAT_LOG%"
    pause
    exit /b 1
)

echo [INFO] Indito naplo: "%BAT_LOG%"
echo [INFO] Elindul az elozetes hardverfelmeres es a teljes telepites...
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SRC_DIR%\setup_ultimate.ps1" %*
set "PS_EXIT=%ERRORLEVEL%"

echo. >> "%BAT_LOG%"
echo PowerShell kilepesi kod: %PS_EXIT% >> "%BAT_LOG%"
echo Befejezes ideje: %DATE% %TIME% >> "%BAT_LOG%"

echo.
echo ====================================================================
if "%PS_EXIT%"=="0" (
    echo [SIKERES] A Plex Subtitler Ultimate telepites sikeresen lefutott!
) else (
    echo [HIBA] A telepites hibaval allt le (kilepesi kod: %PS_EXIT%).
)
echo Indito naplo: "%BAT_LOG%"
echo Reszletes PowerShell naplo: "%LOG_DIR%\install_ultimate_*.log"
echo ====================================================================
echo.
pause
exit /b %PS_EXIT%
