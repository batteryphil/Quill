# Quill — Windows Uninstaller
# Run: powershell -ExecutionPolicy Bypass -File install\uninstall-windows.ps1

$InstallDir = "$env:LOCALAPPDATA\Quill"
$StartMenu  = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Quill"
$Desktop    = [System.Environment]::GetFolderPath("Desktop")

Write-Host "`nQuill -- Uninstall`n" -ForegroundColor Cyan

# Stop server
Get-Process | Where-Object { $_.CommandLine -match "quill" } | Stop-Process -Force -ErrorAction SilentlyContinue

Remove-Item -Force "$Desktop\Quill.lnk"       -ErrorAction SilentlyContinue; Write-Host "  [+] Removed Desktop shortcut"    -ForegroundColor Green
Remove-Item -Recurse -Force $StartMenu          -ErrorAction SilentlyContinue; Write-Host "  [+] Removed Start Menu entry"    -ForegroundColor Green
Remove-Item -Recurse -Force $InstallDir         -ErrorAction SilentlyContinue; Write-Host "  [+] Removed install directory"   -ForegroundColor Green

Write-Host "`n  [OK] Quill uninstalled." -ForegroundColor Green
Write-Host "  Your projects at %USERPROFILE%\.quill\ were NOT removed."
Write-Host "  Delete manually if desired.`n"
