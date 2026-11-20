<#
.SYNOPSIS
    Plex AI Subtitler Ultimate Telepito — 100%-ban automata, szuz Windows gepre keszitett telepito.
.DESCRIPTION
    - Nem igenyel elore telepitett LM Studio-t, llama.cpp-t, Ollama-t, Python-t, FFmpeg-et vagy Tesseract-ot.
    - 0. lepesben elozetes hardver- es internet-felmerest vegez (OS, CPU, RAM, NVIDIA GPU/VRAM/driver, tarhely)
      es dinamikusan kivalasztja a legjobb profilt (CUDA_ULTRA / CUDA_MID / CUDA_ENTRY / CPU_ONLY).
    - Elore feltelepiti es beallitja az OSSZES szukseges komponenst:
      1. Visual C++ Redistributable (x64)
      2. Python 3.12 (WindowsApps MS Store stub kiszuresevel, winget + python.org fallback)
      3. FFmpeg + ffprobe
      4. Tesseract OCR (Blu-ray PGS kepfeliratokhoz)
      5. Ollama (beepitett CUDA llama.cpp motor, winget + OllamaSetup.exe /VERYSILENT fallback,
         OLLAMA_FLASH_ATTENTION=1, automata szerver-inditas es modell letoltes)
      6. uv + .venv + PyTorch (CUDA cu124 + cuBLAS/cuDNN vagy CPU) + requirements.txt + PyInstaller
      6b. Whisper modell elotoltese a helyi cache-be (hogy az elso film is azonnal induljon!)
      7. Hardverhez hangolt settings.json (tiszta UTF-8, BOM nelkul)
      8. Teljes 51-tesztes pytest + elo GPU/Ollama probaforditas (doctor_gpu.py --live-llm)
      9. Onallo PlexSubtitler.exe leforditasa
      10. Asztali parancsikonok es opcionalis Windows Task Scheduler hatterfigyelo
    - Minden lepesrol reszletes log keszul: logs\install_ultimate_YYYYMMDD_HHMMSS.log es logs\hardware_report.json.
#>

param(
    [ValidateSet("AUTO", "CUDA_ULTRA", "CUDA_MID", "CUDA_ENTRY", "CPU_ONLY")]
    [string]$ForceProfile = "AUTO",
    [switch]$EnableAutostart,
    [string[]]$WatchDirs = @(),
    [switch]$SkipTests,
    [switch]$SkipBuild,
    [switch]$SkipWhisperPrefetch
)

$ErrorActionPreference = "Stop"

# Windows 10 PowerShell 5.1 TLS 1.2 kotelezo a GitHub / Python / Ollama letoltesekhez!
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch {}

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
$ResolvedPythonExe = $null
$ResolvedOllamaExe = $null

function Add-StepResult {
    param([string]$Step, [string]$Status, [string]$Detail)
    $StepReport.Add([PSCustomObject]@{
        Lepes   = $Step
        Allapot = $Status
        Reszlet = $Detail
    })
}

function Write-Utf8NoBom {
    param([string]$Path, [string]$Content)
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $utf8NoBom)
}

function Refresh-ProcessPath {
    $machinePath = [System.Environment]::GetEnvironmentVariable("PATH", "Machine")
    $userPath = [System.Environment]::GetEnvironmentVariable("PATH", "User")
    $extras = @(
        "$env:LOCALAPPDATA\Programs\Ollama",
        "$env:LOCALAPPDATA\Programs\Python\Python312",
        "$env:LOCALAPPDATA\Programs\Python\Python312\Scripts",
        "C:\Program Files\Python312",
        "C:\Program Files\Python312\Scripts",
        "C:\Program Files\Tesseract-OCR",
        "$env:LOCALAPPDATA\Microsoft\WinGet\Links"
    ) -join ";"
    $env:PATH = "$machinePath;$userPath;$extras"
}

function Find-RealPython {
    Refresh-ProcessPath
    $candidates = @()
    foreach ($c in (Get-Command python -All -ErrorAction SilentlyContinue)) {
        if ($c.Source -and ($c.Source -notlike "*\WindowsApps\*")) {
            $candidates += $c.Source
        }
    }
    $candidates += @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "C:\Program Files\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "C:\Program Files\Python311\python.exe"
    )
    foreach ($exe in ($candidates | Select-Object -Unique)) {
        if ($exe -and (Test-Path $exe)) {
            $verStr = (& $exe --version 2>&1) -join " "
            if ($LASTEXITCODE -eq 0 -and $verStr -match "Python 3\.(1[1-9]|[2-9][0-9])") {
                return @{ Exe = $exe; Version = $verStr }
            }
        }
    }
    return $null
}

function Find-OllamaExe {
    Refresh-ProcessPath
    $cmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -and (Test-Path $cmd.Source)) {
        return $cmd.Source
    }
    foreach ($p in @(
        "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe",
        "C:\Program Files\Ollama\ollama.exe"
    )) {
        if (Test-Path $p) { return $p }
    }
    return $null
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
    Write-Host "   PLEX AI SUBTITLER v1.3.0 — ULTIMATE TELEPITO (ZERO-CONFIG)" -ForegroundColor Magenta
    Write-Host "   Naplo: $InstallLog" -ForegroundColor Gray
    Write-Host "================================================================" -ForegroundColor Magenta

    # ------------------------------------------------------------------
    # 0. LEPES: ELOZETES HARDVER-, TARHELY- ES INTERNET-FELMERES
    # ------------------------------------------------------------------
    Invoke-CheckedStep -StepName "0. Elozetes hardver-, tarhely- es internet-felmeres" -Action {
        try {
            $null = Invoke-WebRequest -Uri "https://pypi.org" -Method Head -TimeoutSec 8 -UseBasicParsing
        } catch {
            Write-Host "  [FIGYELEM] Internet-ellenorzes sikertelen (pypi.org): folytatas, de uj letolteshez internet kell!" -ForegroundColor Yellow
        }

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

        $minDisk = if ($HwProfile.tier -eq "CUDA_ULTRA") { 20.0 } elseif ($HwProfile.use_cuda) { 14.0 } else { 8.0 }
        if ($HwProfile.disk_free_gb -lt $minDisk) {
            Write-Host "  [FIGYELEM] Keves szabad tarhely ($($HwProfile.disk_free_gb) GB < ajanlott $minDisk GB)!" -ForegroundColor Yellow
        }

        $hwJson = $HwProfile | ConvertTo-Json -Depth 4
        Write-Utf8NoBom -Path (Join-Path $LogsDir "hardware_report.json") -Content $hwJson
        Write-Utf8NoBom -Path (Join-Path $ReleaseLogsDir "hardware_report.json") -Content $hwJson

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
            try {
                winget install --id Microsoft.VCRedist.2015+.x64 -e --silent --accept-source-agreements --accept-package-agreements
            } catch {}
        }
        if (-not (Test-Path $vcDll)) {
            $vcSetup = Join-Path $env:TEMP "vc_redist.x64.exe"
            Invoke-WebRequest -Uri "https://aka.ms/vs/17/release/vc_redist.x64.exe" -OutFile $vcSetup -UseBasicParsing
            Start-Process -FilePath $vcSetup -ArgumentList "/install /quiet /norestart" -Wait
        }
        if (Test-Path $vcDll) { return "VC++ 2015-2022 (x64) sikeresen telepitve" }
        throw "VC++ Redistributable nem erzekelheto (ha PyTorch DLL hiba van, telepitsd kezzel)."
    }

    # ------------------------------------------------------------------
    # 2. LEPES: PYTHON 3.12 ELLENORZESE / AUTOMATA TELEPITESE (STUB-VEDELEMMEL)
    # ------------------------------------------------------------------
    Invoke-CheckedStep -StepName "2. Python 3.12 ellenorzese / automata telepitese" -Action {
        $found = Find-RealPython
        if (-not $found) {
            if (Get-Command winget -ErrorAction SilentlyContinue) {
                try {
                    winget install --id Python.Python.3.12 -e --silent --accept-source-agreements --accept-package-agreements
                } catch {}
                $found = Find-RealPython
            }
        }
        if (-not $found) {
            Write-Host "  Hivatalos Python 3.12 telepito letoltese (python.org)..." -ForegroundColor Gray
            $pySetup = Join-Path $env:TEMP "python-3.12.7-amd64.exe"
            Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe" -OutFile $pySetup -UseBasicParsing
            Start-Process -FilePath $pySetup -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_test=0" -Wait
            $found = Find-RealPython
        }
        if (-not $found) {
            throw "Python 3.12 telepites utan sem talalhato. Inditsd ujra a gepet/ablakot es futtasd ujra!"
        }
        $script:ResolvedPythonExe = $found.Exe
        return "$($found.Version) ($($found.Exe))"
    }

    # ------------------------------------------------------------------
    # 3. LEPES: FFMPEG + FFPROBE ELLENORZESE / TELEPITESE
    # ------------------------------------------------------------------
    Invoke-CheckedStep -StepName "3. FFmpeg es ffprobe ellenorzese / telepitese" -Optional -Action {
        Refresh-ProcessPath
        if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
            if (Get-Command winget -ErrorAction SilentlyContinue) {
                try {
                    winget install --id Gyan.FFmpeg -e --silent --accept-source-agreements --accept-package-agreements
                } catch {}
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
                Invoke-WebRequest -Uri $tessUrl -OutFile $tessSetup -UseBasicParsing
                Start-Process -FilePath $tessSetup -ArgumentList "/S" -Wait
            }
        }
        if (Test-Path $tessDir) {
            if ($env:PATH -notlike "*$tessDir*") { $env:PATH = "$env:PATH;$tessDir" }
            $userPath = [System.Environment]::GetEnvironmentVariable("PATH", "User")
            if (-not $userPath) { $userPath = "" }
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
    # 5. LEPES: OLLAMA (BEEPITETT CUDA LLAMA.CPP MOTOR) + MODELL LETOLTESE
    #    (Nem kell LM Studio vagy kezi llama.cpp!)
    # ------------------------------------------------------------------
    Invoke-CheckedStep -StepName "5. Helyi AI motor (Ollama) telepitese es $($HwProfile.ollama_model) letoltese" -Action {
        # Flash Attention bekapcsolasa a gyorsabb, kisebb VRAM-igenyu 12B futtatasert
        $env:OLLAMA_FLASH_ATTENTION = "1"
        [System.Environment]::SetEnvironmentVariable("OLLAMA_FLASH_ATTENTION", "1", "User")

        $ollamaExe = Find-OllamaExe
        if (-not $ollamaExe) {
            if (Get-Command winget -ErrorAction SilentlyContinue) {
                try {
                    winget install --id Ollama.Ollama -e --silent --accept-source-agreements --accept-package-agreements
                } catch {}
                $ollamaExe = Find-OllamaExe
            }
        }
        if (-not $ollamaExe) {
            Write-Host "  Hivatalos Ollama telepito letoltese (ollama.com)..." -ForegroundColor Gray
            $setupExe = Join-Path $env:TEMP "OllamaSetup.exe"
            Invoke-WebRequest -Uri "https://ollama.com/download/OllamaSetup.exe" -OutFile $setupExe -UseBasicParsing
            # Ollama Windows installer Inno Setup alapu -> /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
            Start-Process -FilePath $setupExe -ArgumentList "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART" -Wait
            $ollamaExe = Find-OllamaExe
        }
        if (-not $ollamaExe) {
            throw "Ollama telepites utan sem talalhato ($env:LOCALAPPDATA\Programs\Ollama\ollama.exe)."
        }
        $script:ResolvedOllamaExe = $ollamaExe

        # Ollama mappa felvetele a User PATH-ba, hogy barmely uj ablakbol elerheto legyen
        $ollamaDir = Split-Path -Parent $ollamaExe
        $userPath = [System.Environment]::GetEnvironmentVariable("PATH", "User")
        if (-not $userPath) { $userPath = "" }
        if ($userPath -notlike "*$ollamaDir*") {
            [System.Environment]::SetEnvironmentVariable("PATH", ($userPath.TrimEnd(";") + ";$ollamaDir"), "User")
        }

        $up = $false
        try {
            Invoke-RestMethod -Uri "http://localhost:11434/api/tags" -TimeoutSec 2 | Out-Null
            $up = $true
        } catch {
            Start-Process -FilePath $ollamaExe -ArgumentList "serve" -WindowStyle Hidden
            for ($i = 0; $i -lt 30; $i++) {
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
        Write-Host "  Helyi AI modell letoltese: $targetModel (ez elso alkalommal par percet vehet igenybe)..." -ForegroundColor Gray
        & $ollamaExe pull $targetModel
        if ($LASTEXITCODE -ne 0) { throw "ollama pull $targetModel sikertelen." }
        $ollamaVer = (& $ollamaExe --version 2>&1) -join " "
        return "$ollamaVer | modell kesz: $targetModel (LM Studio / llama.cpp nem szukseges)"
    }

    # ------------------------------------------------------------------
    # 6. LEPES: UV + .VENV + PYTORCH (CUDA VAGY CPU) + REQUIREMENTS
    # ------------------------------------------------------------------
    Invoke-CheckedStep -StepName "6. Python .venv, PyTorch ($($HwProfile.tier)) es osszes fuggoseg telepitese" -Action {
        $pyExe = $script:ResolvedPythonExe
        if (-not $pyExe) { throw "Nincs feloldott Python 3.12 binaris." }

        & $pyExe -m pip install --upgrade pip uv
        if ($LASTEXITCODE -ne 0) { throw "uv telepites sikertelen." }

        if (-not (Test-Path ".venv")) {
            & $pyExe -m uv venv .venv --python $pyExe
            if ($LASTEXITCODE -ne 0) { throw ".venv letrehozas sikertelen." }
        }

        $venvPy = Join-Path $ScriptDir ".venv\Scripts\python.exe"
        if ($HwProfile.use_cuda) {
            & $pyExe -m uv pip install --python $venvPy --index-url https://download.pytorch.org/whl/cu124 torch
            if ($LASTEXITCODE -ne 0) { throw "PyTorch cu124 telepites sikertelen." }
            & $pyExe -m uv pip install --python $venvPy nvidia-cublas-cu12 nvidia-cudnn-cu12
            if ($LASTEXITCODE -ne 0) { throw "NVIDIA CUDA runtime csomagok telepitese sikertelen." }
        } else {
            & $pyExe -m uv pip install --python $venvPy torch
            if ($LASTEXITCODE -ne 0) { throw "PyTorch CPU telepites sikertelen." }
        }

        & $pyExe -m uv pip install --python $venvPy -r requirements.txt pyinstaller
        if ($LASTEXITCODE -ne 0) { throw "requirements.txt telepites sikertelen." }

        $pkgCheck = & $venvPy -c "import torch, faster_whisper, customtkinter, imageio_ffmpeg, watchdog, google.genai, openai, requests, tkinterdnd2, pgsrip, pytesseract, numpy, PyInstaller; print('torch=' + torch.__version__ + ', cuda=' + str(torch.cuda.is_available()))"
        if ($LASTEXITCODE -ne 0) { throw "Fuggoseg-import ellenorzes sikertelen." }
        return $pkgCheck
    }

    # ------------------------------------------------------------------
    # 6b. LEPES: WHISPER MODELL ELOTOLTESE (AZONNALI ELSO FUTASHOZ)
    # ------------------------------------------------------------------
    if (-not $SkipWhisperPrefetch) {
        Invoke-CheckedStep -StepName "6b. Whisper modell elotoltese ($($HwProfile.whisper_model))" -Optional -Action {
            $venvPy = Join-Path $ScriptDir ".venv\Scripts\python.exe"
            $wModel = $HwProfile.whisper_model
            Write-Host "  Whisper '$wModel' hangfelismero modell letoltese a helyi gyorsitotarba..." -ForegroundColor Gray
            $dlPath = & $venvPy -c "from faster_whisper import download_model; print(download_model('$wModel'))"
            if ($LASTEXITCODE -ne 0) { throw "Whisper modell elotoltese sikertelen (elso futaskor ujra megprobalja)." }
            return "Whisper '$wModel' elotoltve: $dlPath"
        }
    } else {
        Add-StepResult -Step "6b. Whisper modell elotoltese" -Status "KIHAGYVA" -Detail "-SkipWhisperPrefetch kapcsoloval"
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
            Write-Utf8NoBom -Path $cfgPath -Content $SettingsContent
        }
        return "settings.json mentve (Whisper=$($HwProfile.whisper_model)/$($HwProfile.whisper_device), Ollama=$($HwProfile.ollama_model), batch=$($HwProfile.batch_size))"
    }

    # ------------------------------------------------------------------
    # 8. LEPES: AUTOMATA PYTEST + ELO HARDVER & LLM DIAGNOSZTIKA
    # ------------------------------------------------------------------
    if (-not $SkipTests) {
        Invoke-CheckedStep -StepName "8. Automata pytest es elo hardver+Ollama diagnosztika" -Action {
            $venvPy = Join-Path $ScriptDir ".venv\Scripts\python.exe"
            & $venvPy -m pytest -v
            if ($LASTEXITCODE -ne 0) { throw "pytest sikertelen." }
            & $venvPy doctor_gpu.py --live-llm
            if ($LASTEXITCODE -ne 0) { throw "doctor_gpu.py hibat jelzett." }
            return "Minden pytest teszt es elo Ollama probaforditas sikeres"
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

            & (Join-Path $ScriptDir ".venv\Scripts\pyinstaller.exe") @pyiArgs
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
        $FullShortcut.Description = "Plex AI Subtitler teljes vezerlopult"
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
    try {
        Copy-Item -Path $InstallLog -Destination (Join-Path $ReleaseLogsDir (Split-Path -Leaf $InstallLog)) -Force
    } catch {}
}

exit $ExitCode
