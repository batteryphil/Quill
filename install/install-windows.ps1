# =============================================================================
# Quill — Windows Installer
# =============================================================================
#
# Run from the repository root in PowerShell (not cmd):
#   powershell -ExecutionPolicy Bypass -File install\install-windows.ps1
#
# What it does:
#   1. Verifies Python 3.10+ and git
#   2. Creates a venv at %LOCALAPPDATA%\Quill\venv
#   3. Installs Python dependencies
#   4. Converts quill.png → quill.ico (multi-size)
#   5. Creates a launcher batch file
#   6. Creates Desktop + Start Menu shortcuts (.lnk)
#
# No admin/UAC required.
# =============================================================================

$ErrorActionPreference = "Stop"

# ── Colours ──────────────────────────────────────────────────────────────────
function OK   ($msg) { Write-Host "  [+] $msg" -ForegroundColor Green  }
function INFO ($msg) { Write-Host "  --> $msg" -ForegroundColor Cyan   }
function WARN ($msg) { Write-Host "  [!] $msg" -ForegroundColor Yellow }
function FAIL ($msg) { Write-Host "  [x] $msg" -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "  Quill -- Windows Installer" -ForegroundColor Cyan -NoNewline
Write-Host ""
Write-Host ""

# ── Paths ────────────────────────────────────────────────────────────────────

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Definition
$RepoDir    = Split-Path -Parent $ScriptDir
$InstallDir = "$env:LOCALAPPDATA\Quill"
$VenvDir    = "$InstallDir\venv"
$IconSrc    = "$ScriptDir\icons\quill.png"
$IconDst    = "$InstallDir\quill.ico"
$Launcher   = "$InstallDir\quill.bat"
$LogFile    = "$InstallDir\quill-server.log"

INFO "Repo:    $RepoDir"
INFO "Install: $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

# ── 1. Check Python ──────────────────────────────────────────────────────────

Write-Host ""
INFO "Checking dependencies..."

$PythonExe = $null
foreach ($cmd in @("python", "python3", "py")) {
    try {
        $ver = & $cmd -c "import sys; print(sys.version_info[:2])" 2>$null
        $ok  = & $cmd -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $PythonExe = $cmd; break }
    } catch {}
}
if (-not $PythonExe) {
    FAIL "Python 3.10+ not found. Download from https://www.python.org/downloads/"
}
OK "Python: $(& $PythonExe --version)"

try { $null = & git --version 2>$null }
catch { FAIL "git not found. Download from https://git-scm.com/download/win" }
OK "git: $(& git --version)"

# ── 2. Create virtualenv ─────────────────────────────────────────────────────

Write-Host ""
INFO "Setting up Python environment..."
if (-not (Test-Path $VenvDir)) {
    & $PythonExe -m venv $VenvDir
    OK "Created venv at $VenvDir"
} else {
    OK "Existing venv at $VenvDir"
}

$VenvPython = "$VenvDir\Scripts\python.exe"
$VenvPip    = "$VenvDir\Scripts\pip.exe"

# ── 3. Install dependencies ──────────────────────────────────────────────────

INFO "Installing Python dependencies (this may take a minute)..."
& $VenvPip install --quiet --upgrade pip
& $VenvPip install --quiet -r "$RepoDir\requirements.txt"
OK "Dependencies installed"

# ── 4. Create ICO icon ───────────────────────────────────────────────────────

Write-Host ""
INFO "Creating icon..."

# Try to generate ICO using Pillow
$IconScript = @"
from PIL import Image
import pathlib, sys

src  = pathlib.Path(r'$IconSrc')
dst  = pathlib.Path(r'$IconDst')
img  = Image.open(src).convert('RGBA')
sizes = [(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)]
icons = [img.resize(s, Image.LANCZOS) for s in sizes]
icons[0].save(str(dst), format='ICO', sizes=sizes, append_images=icons[1:])
print('ICO created')
"@
try {
    & $VenvPython -c $IconScript
    OK "Icon: $IconDst"
} catch {
    # Fallback: copy PNG
    Copy-Item $IconSrc "$InstallDir\quill.png"
    $IconDst = "$InstallDir\quill.png"
    WARN "Pillow not available — using PNG icon (install Pillow for ICO support)"
}

# ── 5. Create launcher batch file ────────────────────────────────────────────

Write-Host ""
INFO "Creating launcher..."

@"
@echo off
REM Quill Windows Launcher — auto-generated
setlocal

set QUILL_REPO=$RepoDir
set QUILL_VENV=$VenvDir
set QUILL_PORT=8000
set QUILL_LOG=$LogFile

REM Check if server is already running
curl -sf http://127.0.0.1:%QUILL_PORT%/api/projects >nul 2>&1
if %errorlevel% equ 0 goto :open_browser

REM Start server
echo Starting Quill server...
start /B "" "%QUILL_VENV%\Scripts\python.exe" ^
    -m uvicorn backend.main:app ^
    --host 127.0.0.1 --port %QUILL_PORT% ^
    --log-level warning > "%QUILL_LOG%" 2>&1

REM Wait for server
:wait_loop
timeout /t 1 /nobreak >nul
curl -sf http://127.0.0.1:%QUILL_PORT%/api/projects >nul 2>&1
if %errorlevel% neq 0 goto :wait_loop

:open_browser
REM Open in app mode (Chrome > Edge > default browser)
set URL=http://127.0.0.1:%QUILL_PORT%

set CHROME_PATH=
for %%P in (
    "%ProgramFiles%\Google\Chrome\Application\chrome.exe"
    "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
    "%LocalAppData%\Google\Chrome\Application\chrome.exe"
) do (
    if exist %%P ( set CHROME_PATH=%%~P & goto :found_chrome )
)
:found_chrome

if defined CHROME_PATH (
    start "" "%CHROME_PATH%" --app=%URL% --window-size=1400,900
    goto :eof
)

set EDGE_PATH=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe
if exist "%EDGE_PATH%" (
    start "" "%EDGE_PATH%" --app=%URL% --window-size=1400,900
    goto :eof
)

REM Fallback: default browser
start "" %URL%
"@ | Set-Content -Encoding ASCII $Launcher

OK "Launcher: $Launcher"

# ── 6. Create shortcuts ───────────────────────────────────────────────────────

Write-Host ""
INFO "Creating shortcuts..."

$WshShell = New-Object -ComObject WScript.Shell

function New-Shortcut($Target, $ShortcutPath, $Description) {
    $sc              = $WshShell.CreateShortcut($ShortcutPath)
    $sc.TargetPath   = $Target
    $sc.IconLocation = "$IconDst, 0"
    $sc.Description  = $Description
    $sc.WorkingDirectory = $RepoDir
    $sc.Save()
}

# Desktop
$DesktopPath = [System.Environment]::GetFolderPath("Desktop")
New-Shortcut $Launcher "$DesktopPath\Quill.lnk" "Open Quill writing environment"
OK "Desktop shortcut: $DesktopPath\Quill.lnk"

# Start Menu
$StartMenu = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs"
New-Item -ItemType Directory -Force -Path "$StartMenu\Quill" | Out-Null
New-Shortcut $Launcher "$StartMenu\Quill\Quill.lnk" "Open Quill writing environment"
OK "Start Menu: $StartMenu\Quill\Quill.lnk"

# ── Done ─────────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "  [OK] Quill installed successfully!" -ForegroundColor Green
Write-Host ""
Write-Host "  Launch options:" -ForegroundColor Cyan
Write-Host "   * Double-click the Quill icon on your Desktop"
Write-Host "   * Search 'Quill' in the Start Menu"
Write-Host "   * Run from terminal: $Launcher"
Write-Host ""
Write-Host "  Installed to: $InstallDir" -ForegroundColor Cyan
Write-Host "  Server logs:  $LogFile" -ForegroundColor Cyan
Write-Host ""
