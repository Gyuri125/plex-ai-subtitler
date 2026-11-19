<#
.SYNOPSIS
    Plex Subtitler Ultimate Telepito — elozetes hardver-felmeressel, teljes fuggoseg-telepitessel es reszletes naplozassal.
.DESCRIPTION
    - 0. lepesben felmeri a gep hardveret (OS, CPU, RAM, NVIDIA GPU/VRAM/driver, szabad tarhely, internet)
      es dinamikusan kivalasztja az optimalis profilt (CUDA_ULTRA / CUDA_MID / CUDA_ENTRY / CPU_ONLY).
    - Elore feltelepiti az osszes szukseges fuggoseget:
      VC++ Redistributable (x64), Python 3.12, FFmpeg/ffprobe, Tesseract OCR, Ollama + hardverhez illo modell,
      uv + .venv + PyTorch (CUDA vagy CPU) + requirements.txt + PyInstaller.
    - Minden lepesrol es fuggosegrol reszletes logot ir: logs\install_ultimate_YYYYMMDD_HHMMSS.log
      es menti a hardver-riportot: logs\hardware_report.json.
    - Lefuttatja a pytest teszteket, a doctor_gpu.py diagnosztikat, leforditja a PlexSubtitler.exe-t az 1.1 mappaba,
      es letrehozza az asztali parancsikonokat (opcionalisan a Task Scheduler hatterfigyelot is).
#>

param(
    [ValidateSet("AUTO", "CUDA_ULTRA", "CUDA_MID", "CUDA_ENTRY", "CPU_ONLY")]
    [string]$ForceProfile = "AUTO",
    [switch]$EnableAutostart,
    [string[]]$WatchDirs = @(),
    [switch]$SkipTests,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

$ParentCheck = Join-Path (Split-Path -Parent $ScriptDir) "..\1.1"
if (Test-Path $ParentCheck) {
    $ReleaseDir = (Resolve-Path $ParentCheck).Path
} else {
    $ReleaseDir = Join-Path $ScriptDir "release"
}

$LogsDir = Join-Path $ScriptDir "logs"
New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null
$ReleaseLogsDir = Join-Path $ReleaseDir "logs"
New-Item -ItemType Directory -Force -Path $ReleaseLogsDir | Out-Null

$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$InstallLog = Join-Path $LogsDir "install_ultimate_$Timestamp.log"
Start-Transcript -Path $InstallLog -Append | Out-Null

$StepReport = [System.Collections.Generic.List[PSCustomObject]]::new()
$ExitCode = 0

function Add-StepResult {
    param([string]$Step, [string]$Status, [string]$Detail)
    $StepReport.Add([PSCustomObject]@{
        Lepes   = $Step
        Allapot = $Status
        Reszlet = $Detail
    })
}

function Refresh-ProcessPath {
    $machinePath = [System.Environment]::GetEnvironmentVariable("PATH", "Machine")
    $userPath = [System.Environment]::GetEnvironmentVariable("PATH", "User")
    $extras = @(
        "$env:LOCALAPPDATA\Programs\Ollama",
        "$env:LOCALAPPDATA\Programs\Python\Python312",
        "$env:LOCALAPPDATA\Programs\Python\Python312\Scripts",
        "C:\Program Files\Tesseract-OCR",
        "$env:LOCALAPPDATA\Microsoft\WinGet\Links"
    ) -join ";"
    $env:PATH = "$machinePath;$userPath;$extras"
}

function Invoke-CheckedStep {
    param(
        [string]$StepName,
        [scriptblock]$Action,
        [switch]$Optional
    )
    Write-Host "`n>>> $StepName ..." -ForegroundColor Cyan
    try {
        $msg = & $Action
        if (-not $msg) { $msg = "Rendben lefutott" }
        Add-StepResult -Step $StepName -Status "SIKERES" -Detail "$msg"
        Write-Host "[SIKERES] $StepName — $msg" -ForegroundColor Green
    } catch {
        if ($Optional) {
            Add-StepResult -Step $StepName -Status "FIGYELEM" -Detail "$($_.Exception.Message)"
            Write-Host "[FIGYELEM] $StepName — $($_.Exception.Message)" -ForegroundColor Yellow
        } else {
            Add-StepResult -Step $StepName -Status "HIBA" -Detail "$($_.Exception.Message)"
            Write-Host "[HIBA] $StepName — $($_.Exception.Message)" -ForegroundColor Red
            throw
        }
    }
}

# Dinamikus hardverprofil valtozok (0. lepes tolti ki)
$HwProfile = [ordered]@{
    tier            = "CPU_ONLY"
    use_cuda        = $false
    gpu_name        = "Nincs NVIDIA GPU"
    vram_gb         = 0.0
    nvidia_driver   = ""
    cpu_name        = ""
    cpu_cores       = 0
    ram_total_gb    = 0.0
    ram_free_gb     = 0.0
    disk_free_gb    = 0.0
    os_caption      = ""
    whisper_model   = "small"
    whisper_device  = "cpu"
    whisper_compute = "int8"
    ollama_model    = "gemma3:4b"
    batch_size      = 6
    detected_at     = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
}

try {
    Write-Host "================================================================" -ForegroundColor Magenta
    Write-Host "   PLEX SUBTITLER v1.3.0 — ULTIMATE TELEPITO ES HARDVERFELMERO" -ForegroundColor Magenta
    Write-Host "   Naplo: $InstallLog" -ForegroundColor Gray
    Write-Host "================================================================" -ForegroundColor Magenta

    # ------------------------------------------------------------------
    # 0. LEPES: ELOZETES HARDVER- ES RENDSZERFELMERES
    # ------------------------------------------------------------------
    Invoke-CheckedStep -StepName "0. Elozetes hardver- es rendszerfelmeres" -Action {
        $os = Get-CimInstance Win32_OperatingSystem
        $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
        $cs = Get-CimInstance Win32_ComputerSystem

        $HwProfile.os_caption = "$($os.Caption) ($($os.Version))"
        $HwProfile.cpu_name = ($cpu.Name).Trim()
        $HwProfile.cpu_cores = [int]$cpu.NumberOfLogicalProcessors
        $HwProfile.ram_total_gb = [math]::Round($cs.TotalPhysicalMemory / 1GB, 1)
        $HwProfile.ram_free_gb = [math]::Round(($os.FreePhysicalMemory * 1KB) / 1GB, 1)

        $driveLetter = (Split-Path -Qualifier $ScriptDir).TrimEnd(":")
        $disk = Get-PSDrive -Name $driveLetter
        $HwProfile.disk_free_gb = [math]::Round($disk.Free / 1GB, 1)

        # NVIDIA GPU + VRAM detektalas nvidia-smi segitsegevel
        $smiCmd = Get-Command nvidia-smi -ErrorAction SilentlyContinue
        if (-not $smiCmd -and (Test-Path "C:\Windows\System32\nvidia-smi.exe")) {
            $smiCmd = "C:\Windows\System32\nvidia-smi.exe"
        }
        if ($smiCmd) {
            $smiExe = if ($smiCmd -is [string]) { $smiCmd } else { $smiCmd.Source }
            $smiOut = & $smiExe --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits 2>$null
            if ($LASTEXITCODE -eq 0 -and $smiOut) {
                $firstLine = ($smiOut | Select-Object -First 1).ToString()
                $parts = $firstLine.Split(",") | ForEach-Object { $_.Trim() }
                if ($parts.Count -ge 2) {
                    $HwProfile.gpu_name = $parts[0]
                    $HwProfile.vram_gb = [math]::Round(([double]$parts[1]) / 1024.0, 1)
                    if ($parts.Count -ge 3) { $HwProfile.nvidia_driver = $parts[2] }
                }
            }
        }

        # Profil kivalasztasa a mert hardver (vagy -ForceProfile) alapjan
        $chosenTier = $ForceProfile
        if ($chosenTier -eq "AUTO") {
            if ($HwProfile.vram_gb -ge 11.0) {
                $chosenTier = "CUDA_ULTRA"
            } elseif ($HwProfile.vram_gb -ge 7.5) {
                $chosenTier = "CUDA_MID"
            } elseif ($HwProfile.vram_gb -ge 4.0) {
                $chosenTier = "CUDA_ENTRY"
            } else {
                $chosenTier = "CPU_ONLY"
            }
        }

        $HwProfile.tier = $chosenTier
        switch ($chosenTier) {
            "CUDA_ULTRA" {
                $HwProfile.use_cuda = $true
                $HwProfile.whisper_model = "large-v3"
                $HwProfile.whisper_device = "cuda"
                $HwProfile.whisper_compute = "float16"
                $HwProfile.ollama_model = "gemma3:12b"
                $HwProfile.batch_size = 12
            }
            "CUDA_MID" {
                $HwProfile.use_cuda = $true
                $HwProfile.whisper_model = "large-v3"
                $HwProfile.whisper_device = "cuda"
                $HwProfile.whisper_compute = "float16"
                $HwProfile.ollama_model = "gemma3:4b"
                $HwProfile.batch_size = 10
            }
            "CUDA_ENTRY" {
                $HwProfile.use_cuda = $true
                $HwProfile.whisper_model = "medium"
                $HwProfile.whisper_device = "cuda"
                $HwProfile.whisper_compute = "float16"
                $HwProfile.ollama_model = "gemma3:4b"
                $HwProfile.batch_size = 8
            }
            default {
                $HwProfile.use_cuda = $false
                $HwProfile.whisper_model = "small"
                $HwProfile.whisper_device = "cpu"
                $HwProfile.whisper_compute = "int8"
                $HwProfile.ollama_model = if ($HwProfile.ram_total_gb -ge 12.0) { "gemma3:4b" } else { "gemma3:1b" }
                $HwProfile.batch_size = 6
            }
        }

        Write-Host "  OS:             $($HwProfile.os_caption)"
        Write-Host "  CPU:            $($HwProfile.cpu_name) ($($HwProfile.cpu_cores) logikai mag)"
        Write-Host "  RAM:            $($HwProfile.ram_total_gb) GB osszes / $($HwProfile.ram_free_gb) GB szabad"
        Write-Host "  GPU:            $($HwProfile.gpu_name) ($($HwProfile.vram_gb) GB VRAM, driver: $($HwProfile.nvidia_driver))"
        Write-Host "  Szabad tarhely: $($HwProfile.disk_free_gb) GB ($driveLetter`:)"
        Write-Host "  Valasztott AI:  $($HwProfile.tier) -> Whisper=$($HwProfile.whisper_model) ($($HwProfile.whisper_device)), Ollama=$($HwProfile.ollama_model), batch=$($HwProfile.batch_size)" -ForegroundColor Yellow

        $minDisk = if ($HwProfile.tier -eq "CUDA_ULTRA") { 18.0 } elseif ($HwProfile.use_cuda) { 12.0 } else { 7.0 }
        if ($HwProfile.disk_free_gb -lt $minDisk) {
            Write-Host "  [FIGYELEM] Keves szabad tarhely ($($HwProfile.disk_free_gb) GB < ajanlott $minDisk GB)!" -ForegroundColor Yellow
        }

        $hwJson = $HwProfile | ConvertTo-Json -Depth 4
        Set-Content -Path (Join-Path $LogsDir "hardware_report.json") -Value $hwJson -Encoding UTF8
        Set-Content -Path (Join-Path $ReleaseLogsDir "hardware_report.json") -Value $hwJson -Encoding UTF8

        return "$($HwProfile.tier) | GPU=$($HwProfile.gpu_name) ($($HwProfile.vram_gb) GB) | RAM=$($HwProfile.ram_total_gb) GB | Szabad=$($HwProfile.disk_free_gb) GB"
    }

    # ------------------------------------------------------------------
    # 1. LEPES: MICROSOFT VISUAL C++ REDISTRIBUTABLE (x64)
    # ------------------------------------------------------------------
    Invoke-CheckedStep -StepName "1. Visual C++ Redistributable (x64) ellenorzese" -Optional -Action {
        $vcDll = "C:\Windows\System32\vcruntime140_1.dll"
        if (Test-Path $vcDll) {
            return "Mar telepitve (vcruntime140_1.dll jelen van)"
        }
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            winget install --id Microsoft.VCRedist.2015+.x64 -e --silent --accept-source-agreements --accept-package-agreements
        }
        if (-not (Test-Path $vcDll)) {
            $vcSetup = Join-Path $env:TEMP "vc_redist.x64.exe"
            Invoke-WebRequest -Uri "https://aka.ms/vs/17/release/vc_redist.x64.exe" -OutFile $vcSetup
            Start-Process -FilePath $vcSetup -ArgumentList "/install /quiet /norestart" -Wait
        }
        if (Test-Path $vcDll) { return "VC++ 2015-2022 (x64) sikeresen telepitve" }
        throw "VC++ Redistributable nem erzekelheto (ha PyTorch DLL hiba van, telepitsd kezzel)."
    }

    # ------------------------------------------------------------------
    # 2. LEPES: PYTHON 3.12 ELLENORZESE / AUTOMATA TELEPITESE
    # ------------------------------------------------------------------
    Invoke-CheckedStep -StepName "2. Python 3.11+ ellenorzese / automata telepitese" -Action {
        Refresh-ProcessPath
        $pyCmd = Get-Command python -ErrorAction SilentlyContinue
        $needInstall = $true
        if ($pyCmd) {
            $verStr = (& python --version 2>&1) -join " "
            if ($LASTEXITCODE -eq 0 -and $verStr -match "Python 3\.(1[1-9]|[2-9][0-9])") {
                $needInstall = $false
                return $verStr
            }
        }
        if ($needInstall) {
            if (Get-Command winget -ErrorAction SilentlyContinue) {
                winget install --id Python.Python.3.12 -e --silent --accept-source-agreements --accept-package-agreements
            } else {
                $pySetup = Join-Path $env:TEMP "python-3.12.7-amd64.exe"
                Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe" -OutFile $pySetup
                Start-Process -FilePath $pySetup -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_test=0" -Wait
            }
            Refresh-ProcessPath
            $verStr = (& python --version 2>&1) -join " "
            if ($LASTEXITCODE -ne 0) {
                throw "Python 3.12 telepites utan sem erheto el a PATH-ban. Inditsd ujra a terminalt es futtasd ujra!"
            }
            return "Telepitve: $verStr"
        }
    }

    # ------------------------------------------------------------------
    # 3. LEPES: FFMPEG + FFPROBE ELLENORZESE / TELEPITESE
    # ------------------------------------------------------------------
    Invoke-CheckedStep -StepName "3. FFmpeg es ffprobe ellenorzese / telepitese" -Optional -Action {
        Refresh-ProcessPath
        if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
            if (Get-Command winget -ErrorAction SilentlyContinue) {
                winget install --id Gyan.FFmpeg -e --silent --accept-source-agreements --accept-package-agreements
                Refresh-ProcessPath
            }
        }
        if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
            $first = (& ffmpeg -version 2>&1 | Select-Object -First 1).ToString()
            return $first
        }
        return "Rendszer FFmpeg nincs a PATH-ban (a beepitett imageio-ffmpeg fog futni fallbackkent)."
    }

    # ------------------------------------------------------------------
    # 4. LEPES: TESSERACT OCR (BLU-RAY PGS KEPFELIRATHOZ)
    # ------------------------------------------------------------------
    Invoke-CheckedStep -StepName "4. Tesseract OCR telepitese (Blu-ray PGS felirathoz)" -Optional -Action {
        $tessDir = "C:\Program Files\Tesseract-OCR"
        $tessExe = Join-Path $tessDir "tesseract.exe"
        if (-not (Get-Command tesseract -ErrorAction SilentlyContinue) -and -not (Test-Path $tessExe)) {
            if (Get-Command winget -ErrorAction SilentlyContinue) {
                try {
                    winget install --id UB-Mannheim.TesseractOCR -e --silent --accept-source-agreements --accept-package-agreements
                } catch {}
            }
            if (-not (Test-Path $tessExe)) {
                $tessUrl = "https://github.com/UB-Mannheim/tesseract/releases/download/v5.4.0.20240606/tesseract-ocr-w64-setup-5.4.0.20240606.exe"
                $tessSetup = Join-Path $env:TEMP "tesseract-setup.exe"
                Invoke-WebRequest -Uri $tessUrl -OutFile $tessSetup
                Start-Process -FilePath $tessSetup -ArgumentList "/S" -Wait
            }
        }
        if (Test-Path $tessDir) {
            if ($env:PATH -notlike "*$tessDir*") { $env:PATH = "$env:PATH;$tessDir" }
            $userPath = [System.Environment]::GetEnvironmentVariable("PATH", "User")
            if ($userPath -notlike "*$tessDir*") {
                [System.Environment]::SetEnvironmentVariable("PATH", ($userPath.TrimEnd(";") + ";$tessDir"), "User")
            }
        }
        if (Get-Command tesseract -ErrorAction SilentlyContinue) {
            return (& tesseract --version 2>&1 | Select-Object -First 1).ToString()
        }
        if (Test-Path $tessExe) {
            return (& $tessExe --version 2>&1 | Select-Object -First 1).ToString()
        }
        throw "Tesseract nincs telepitve (PGS OCR helyett Whisper fog futni)."
    }

    # ------------------------------------------------------------------
    # 5. LEPES: OLLAMA TELEPITESE, INDITASA ES MODELL LETOLTESE
    # ------------------------------------------------------------------
    Invoke-CheckedStep -StepName "5. Ollama telepitese es $($HwProfile.ollama_model) letoltese" -Action {
        Refresh-ProcessPath
        if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
            if (Get-Command winget -ErrorAction SilentlyContinue) {
                winget install --id Ollama.Ollama -e --silent --accept-source-agreements --accept-package-agreements
            } else {
                $setupExe = Join-Path $env:TEMP "OllamaSetup.exe"
                Invoke-WebRequest -Uri "https://ollama.com/download/OllamaSetup.exe" -OutFile $setupExe
                Start-Process -FilePath $setupExe -ArgumentList "/S" -Wait
            }
            Refresh-ProcessPath
        }
        if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
            throw "Ollama binaris telepites utan sem erheto el."
        }

        $up = $false
        try {
            Invoke-RestMethod -Uri "http://localhost:11434/api/tags" -TimeoutSec 2 | Out-Null
            $up = $true
        } catch {
            Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden
            for ($i = 0; $i -lt 25; $i++) {
                Start-Sleep -Seconds 1
                try {
                    Invoke-RestMethod -Uri "http://localhost:11434/api/tags" -TimeoutSec 2 | Out-Null
                    $up = $true
                    break
                } catch {}
            }
        }
        if (-not $up) { throw "Ollama szerver nem indult el a 11434 porton." }

        $targetModel = $HwProfile.ollama_model
        & ollama pull $targetModel
        if ($LASTEXITCODE -ne 0) { throw "ollama pull $targetModel sikertelen." }
        $ollamaVer = (& ollama --version 2>&1) -join " "
        return "$ollamaVer | modell kesz: $targetModel"
    }

    # ------------------------------------------------------------------
    # 6. LEPES: UV + .VENV + PYTORCH (CUDA VAGY CPU) + REQUIREMENTS
    # ------------------------------------------------------------------
    Invoke-CheckedStep -StepName "6. Python .venv, PyTorch ($($HwProfile.tier)) es osszes fuggoseg telepitese" -Action {
        if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
            python -m pip install --upgrade uv
            if ($LASTEXITCODE -ne 0) { throw "uv telepites sikertelen." }
            Refresh-ProcessPath
        }
        if (-not (Test-Path ".venv")) {
            uv venv .venv --python 3.12
            if ($LASTEXITCODE -ne 0) { throw ".venv letrehozas sikertelen." }
        }

        if ($HwProfile.use_cuda) {
            uv pip install --python .venv\Scripts\python.exe --index-url https://download.pytorch.org/whl/cu124 torch
            if ($LASTEXITCODE -ne 0) { throw "PyTorch cu124 telepites sikertelen." }
            uv pip install --python .venv\Scripts\python.exe nvidia-cublas-cu12 nvidia-cudnn-cu12
            if ($LASTEXITCODE -ne 0) { throw "NVIDIA CUDA runtime csomagok telepitese sikertelen." }
        } else {
            uv pip install --python .venv\Scripts\python.exe torch
            if ($LASTEXITCODE -ne 0) { throw "PyTorch CPU telepites sikertelen." }
        }

        uv pip install --python .venv\Scripts\python.exe -r requirements.txt pyinstaller
        if ($LASTEXITCODE -ne 0) { throw "requirements.txt telepites sikertelen." }

        $pkgCheck = & .venv\Scripts\python.exe -c "import torch, faster_whisper, customtkinter, imageio_ffmpeg, watchdog, google.genai, openai, requests, tkinterdnd2, pgsrip, pytesseract, numpy, PyInstaller; print('torch=' + torch.__version__ + ', cuda=' + str(torch.cuda.is_available()))"
        if ($LASTEXITCODE -ne 0) { throw "Fuggoseg-import ellenorzes sikertelen." }
        return $pkgCheck
    }

    # ------------------------------------------------------------------
    # 7. LEPES: HARDVERHEZ IGAZITOTT SETTINGS.JSON GENERALASA
    # ------------------------------------------------------------------
    Invoke-CheckedStep -StepName "7. Hardverhez optimalizalt settings.json kiirasa ($($HwProfile.tier))" -Action {
        New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null

        $watchJson = if ($WatchDirs.Count -gt 0) {
            ($WatchDirs | ConvertTo-Json -Compress)
        } else {
            "[]"
        }
        if ($WatchDirs.Count -eq 1) { $watchJson = "[$watchJson]" }

        $SettingsContent = @"
{
    "app1": {
        "watch_dirs": $watchJson,
        "temp_audio_dir": "./temp_audio",
        "ffmpeg_path": "ffmpeg",
        "audio_format": "wav",
        "audio_sample_rate": 16000,
        "audio_stream_index": 0,
        "preferred_audio_language": "auto",
        "extract_embedded_subtitles": true,
        "enable_pgs_ocr": true,
        "enable_opensubtitles": false,
        "opensubtitles_api_key": "",
        "preferred_subtitle_language": "auto",
        "confirm_streams_on_drop": false,
        "pause_when_gaming": true,
        "auto_translate_after_extract": true,
        "unload_whisper_before_translate": true,
        "scan_existing_on_start": true,
        "whisper_model": "$($HwProfile.whisper_model)",
        "whisper_language": null,
        "whisper_device": "$($HwProfile.whisper_device)",
        "whisper_compute_type": "$($HwProfile.whisper_compute)",
        "watchdog_poll_interval": 5,
        "processed_log": "./processed_files.log",
        "cleanup_audio": true,
        "log_level": "INFO"
    },
    "app2": {
        "llm_provider": "local",
        "llm_api_key": "",
        "llm_model": "$($HwProfile.ollama_model)",
        "llm_base_url": "http://localhost:11434/v1",
        "translation_batch_size": $($HwProfile.batch_size),
        "source_language": "auto",
        "target_language": "Hungarian",
        "translation_style_hint": "Use natural, colloquial language as used in Hungarian movie dubbing.",
        "api_request_delay": 0.0,
        "format_tv_subtitles": true,
        "max_chars_per_line": 42,
        "max_lines_per_block": 2,
        "auto_start_ollama": true,
        "auto_pull_ollama_model": true,
        "auto_movie_context": true,
        "ollama_keep_alive": "5m",
        "plex_refresh_enabled": false,
        "plex_url": "http://localhost:32400",
        "plex_token": "",
        "auto_align_subtitles": false,
        "desktop_notifications": true,
        "notification_sound": true,
        "ui_mode": "simple",
        "log_level": "INFO"
    }
}
"@
        foreach ($cfgPath in @((Join-Path $ScriptDir "settings.json"), (Join-Path $ReleaseDir "settings.json"))) {
            if (Test-Path $cfgPath) {
                Copy-Item -Path $cfgPath -Destination "$cfgPath.pre_install.bak" -Force
            }
            Set-Content -Path $cfgPath -Value $SettingsContent -Encoding UTF8
        }
        return "settings.json mentve (Whisper=$($HwProfile.whisper_model)/$($HwProfile.whisper_device), Ollama=$($HwProfile.ollama_model), batch=$($HwProfile.batch_size))"
    }

    # ------------------------------------------------------------------
    # 8. LEPES: AUTOMATA PYTEST + ELO HARDVER & LLM DIAGNOSZTIKA
    # ------------------------------------------------------------------
    if (-not $SkipTests) {
        Invoke-CheckedStep -StepName "8. Automata pytest es elo hardver+Ollama diagnosztika" -Action {
            & .venv\Scripts\python.exe -m pytest -v
            if ($LASTEXITCODE -ne 0) { throw "pytest sikertelen." }
            & .venv\Scripts\python.exe doctor_gpu.py --live-llm
            if ($LASTEXITCODE -ne 0) { throw "doctor_gpu.py hibat jelzett." }
            return "Minden pytest teszt es elo Ollama proba sikeres"
        }
    } else {
        Add-StepResult -Step "8. Automata pytest es diagnosztika" -Status "KIHAGYVA" -Detail "-SkipTests kapcsoloval"
    }

    # ------------------------------------------------------------------
    # 9. LEPES: PLEXSUBTITLER.EXE BUILDELESE ES KIHELYEZESE
    # ------------------------------------------------------------------
    if (-not $SkipBuild) {
        Invoke-CheckedStep -StepName "9. PlexSubtitler.exe buildelese ($($HwProfile.tier))" -Action {
            $pyiArgs = @(
                "--noconfirm", "--clean", "--windowed",
                "--name", "PlexSubtitler",
                "--icon", "assets\app.ico",
                "--add-data", "assets;assets",
                "--collect-all", "customtkinter",
                "--collect-all", "tkinterdnd2",
                "--collect-all", "faster_whisper",
                "--collect-all", "imageio_ffmpeg",
                "--collect-data", "certifi"
            )
            if ($HwProfile.use_cuda) {
                $pyiArgs += @("--collect-all", "nvidia.cublas", "--collect-all", "nvidia.cudnn")
            }
            $pyiArgs += "main.py"

            & .venv\Scripts\pyinstaller.exe @pyiArgs
            if ($LASTEXITCODE -ne 0) { throw "PyInstaller build sikertelen." }

            if (Test-Path (Join-Path $ReleaseDir "_internal")) {
                Remove-Item -Recurse -Force (Join-Path $ReleaseDir "_internal")
            }
            Copy-Item -Recurse -Force (Join-Path $ScriptDir "dist\PlexSubtitler\*") $ReleaseDir
            Remove-Item -Recurse -Force (Join-Path $ScriptDir "build"), (Join-Path $ScriptDir "dist")
            return "Build kesz: $ReleaseDir\PlexSubtitler.exe"
        }
    } else {
        Add-StepResult -Step "9. PlexSubtitler.exe buildelese" -Status "KIHAGYVA" -Detail "-SkipBuild kapcsoloval"
    }

    # ------------------------------------------------------------------
    # 10. LEPES: ASZTALI IKONOK ES OPCIONALIS TASK SCHEDULER
    # ------------------------------------------------------------------
    Invoke-CheckedStep -StepName "10. Asztali parancsikonok es Task Scheduler beallitasa" -Action {
        $Desktop = [Environment]::GetFolderPath("Desktop")
        $ExePath = Join-Path $ReleaseDir "PlexSubtitler.exe"
        $WshShell = New-Object -ComObject WScript.Shell

        $QuickShortcut = $WshShell.CreateShortcut((Join-Path $Desktop "Film feliratozas (Huzd ide).lnk"))
        $QuickShortcut.TargetPath = $ExePath
        $QuickShortcut.Arguments = "--quick"
        $QuickShortcut.WorkingDirectory = $ReleaseDir
        $QuickShortcut.IconLocation = "$ExePath,0"
        $QuickShortcut.Description = "Huzz ra egy .mkv, .mp4 filmet vagy sorozat mappat magyar felirat keszitesehez"
        $QuickShortcut.Save()

        $FullShortcut = $WshShell.CreateShortcut((Join-Path $Desktop "Plex Subtitler.lnk"))
        $FullShortcut.TargetPath = $ExePath
        $FullShortcut.WorkingDirectory = $ReleaseDir
        $FullShortcut.IconLocation = "$ExePath,0"
        $FullShortcut.Description = "Plex Subtitler teljes vezerlopult"
        $FullShortcut.Save()

        if ($EnableAutostart) {
            $action = New-ScheduledTaskAction -Execute $ExePath -Argument "--cli --app both --scan-existing" -WorkingDirectory $ReleaseDir
            $trigger = New-ScheduledTaskTrigger -AtLogOn
            $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 0)
            Register-ScheduledTask -TaskName "PlexSubtitlerAutoWatcher" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
            return "Asztali ikonok + Task Scheduler hatterfigyelo beallitva (jatek kozben szunetel)"
        }
        return "Asztali ikonok elkeszultek (Task Scheduler a GUI-ban barmikor bekapcsolhato)"
    }
}
catch {
    $ExitCode = 1
    Write-Host "`n[VEGZETES TELEPITESI HIBA] $($_.Exception.Message)" -ForegroundColor Red
}
finally {
    Write-Host "`n================================================================" -ForegroundColor Yellow
    Write-Host "TELEPITESI OSSZEGZES (Hardver: $($HwProfile.tier))" -ForegroundColor Yellow
    Write-Host "================================================================" -ForegroundColor Yellow
    $StepReport | Format-Table -AutoSize
    Write-Host "Reszletes naplo: $InstallLog" -ForegroundColor Cyan
    Write-Host "Hardver riport:  $(Join-Path $LogsDir 'hardware_report.json')" -ForegroundColor Cyan
    try { Stop-Transcript | Out-Null } catch {}
    # Masoljuk at a kesz telepitesi logot az 1.1\logs mappaba is, hogy a Hibacsomag (.zip) automatikusan megtalalja!
    try {
        Copy-Item -Path $InstallLog -Destination (Join-Path $ReleaseLogsDir (Split-Path -Leaf $InstallLog)) -Force
    } catch {}
}

exit $ExitCode
