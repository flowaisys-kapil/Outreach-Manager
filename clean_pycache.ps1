# clean_pycache.ps1
# Removes all __pycache__ folders and compiled .pyc/.pyo files from project root

$PSScriptRoot = Split-Path -Parent -Path $MyInvocation.MyCommand.Definition
Set-Location -Path $PSScriptRoot

Write-Host "==========================================" -ForegroundColor Green
Write-Host "        REMOVING PYCACHE FILES            " -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Green
Write-Host ""

$cacheDirs = Get-ChildItem -Path $PSScriptRoot -Recurse -Filter "__pycache__" -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -notmatch '\\\.venv\\' }

foreach ($dir in $cacheDirs) {
    Write-Host "Removing: $($dir.FullName)" -ForegroundColor Yellow
    Remove-Item -Path $dir.FullName -Recurse -Force -ErrorAction SilentlyContinue
}

$pycFiles = Get-ChildItem -Path $PSScriptRoot -Recurse -Include "*.pyc", "*.pyo" -File -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -notmatch '\\\.venv\\' }

foreach ($file in $pycFiles) {
    Remove-Item -Path $file.FullName -Force -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "Clean completed successfully." -ForegroundColor Green
