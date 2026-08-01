# create_desktop_shortcut.ps1
# Creates a Desktop shortcut for Outreach Manager with a custom icon.

$PSScriptRoot = Split-Path -Parent -Path $MyInvocation.MyCommand.Definition
$scriptPath = Join-Path $PSScriptRoot "run_outreach_manager.ps1"
$iconPath = Join-Path $PSScriptRoot "archive\logo.ico"

$desktopPath = [System.Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktopPath "Outreach Manager.lnk"

Write-Host "Creating desktop shortcut..." -ForegroundColor Cyan

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($shortcutPath)
$Shortcut.TargetPath = "powershell.exe"
$Shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`""
$Shortcut.WorkingDirectory = $PSScriptRoot
$Shortcut.IconLocation = $iconPath
$Shortcut.Description = "Launch Outreach Manager Campaign and CRM Dashboard"
$Shortcut.Save()

Write-Host "Shortcut created successfully on your Desktop!" -ForegroundColor Green
Write-Host "You can now run Outreach Manager by double-clicking the 'Outreach Manager' icon on your desktop." -ForegroundColor Yellow
