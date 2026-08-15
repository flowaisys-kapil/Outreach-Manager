# run_outreach_manager.ps1
# Unified launcher for Outreach Manager
# Connects to the user's NATIVE Chrome window - uses real LinkedIn credentials
# that are already saved in the browser's cookies / localStorage.

$PSScriptRoot = Split-Path -Parent -Path $MyInvocation.MyCommand.Definition
Set-Location -Path $PSScriptRoot

Write-Host "==========================================" -ForegroundColor Green
Write-Host "         LAUNCHING OUTREACH MANAGER       " -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Green

# 1. Determine Chrome executable path
$chromePath = "C:\Program Files\Google\Chrome\Application\chrome.exe"
if (-not (Test-Path $chromePath)) {
    $chromePath = "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
}
if (-not (Test-Path $chromePath)) {
    $chromePath = "chrome.exe"  # Fall back to PATH
}

# 2. Real user Chrome profile - carries existing LinkedIn login cookies
$realProfile = "$env:LOCALAPPDATA\Google\Chrome\User Data"
Write-Host "Native Chrome profile: $realProfile" -ForegroundColor Cyan

# 3. Check and clean up any existing process on port 9222 (e.g. headless/zombie Chrome)
$portOpen = $false
try {
    $connection = Test-NetConnection -ComputerName 127.0.0.1 -Port 9222 -WarningAction SilentlyContinue
    $portOpen = $connection.TcpTestSucceeded
} catch {
    $portOpen = $false
}

if ($portOpen) {
    # Find process ID owning port 9222 and terminate it to prevent background/zombie conflicts
    $conn = Get-NetTCPConnection -LocalPort 9222 -ErrorAction SilentlyContinue
    if ($conn) {
        $debugPid = $conn.OwningProcess | Select-Object -First 1
        if ($debugPid) {
            Write-Host "Cleaning up old/zombie debug process on port 9222 (PID: $debugPid)..." -ForegroundColor Yellow
            Stop-Process -Id $debugPid -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 2
            $portOpen = $false
        }
    }
}

if ($portOpen) {
    Write-Host "Chrome debugging port 9222 is already active - reusing existing instance." -ForegroundColor Green
} else {
    # Check if Chrome is running WITHOUT the debug port
    $chromeRunning = (Get-Process -Name "chrome" -ErrorAction SilentlyContinue) -ne $null
    if ($chromeRunning) {
        Write-Host "Chrome is running but without debug port. Closing and relaunching with debug port..." -ForegroundColor Yellow
        Write-Host "Your tabs will be restored automatically (Chrome session restore)." -ForegroundColor Cyan
        # Gracefully close Chrome so session is saved
        Stop-Process -Name "chrome" -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }

    Write-Host "Launching Chrome (native profile + stealth flags) on port 9222..." -ForegroundColor Green

    $chromeArgs = @(
        "--remote-debugging-port=9222",
        "--user-data-dir=`"$realProfile`"",
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        "--no-first-run",
        "--no-default-browser-check",
        "--metrics-recording-only",
        "--use-mock-keychain",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-hang-monitor",
        "--disable-ipc-flooding-protection",
        "--password-store=basic",
        "--window-size=1920,1080"
    )
    Start-Process $chromePath -ArgumentList $chromeArgs

    Write-Host "Waiting for Chrome native instance to initialise on port 9222..." -ForegroundColor Cyan
    $attempts = 0
    while (-not $portOpen -and $attempts -lt 20) {
        Start-Sleep -Seconds 1
        $attempts++
        try {
            $connection = Test-NetConnection -ComputerName 127.0.0.1 -Port 9222 -WarningAction SilentlyContinue
            $portOpen = $connection.TcpTestSucceeded
        } catch {
            $portOpen = $false
        }
    }
    if (-not $portOpen) {
        Write-Host "WARNING: Chrome debugging port did not activate in 20 seconds. Connection may fail." -ForegroundColor Red
    } else {
        Write-Host "Chrome ready - connected to your native browser session." -ForegroundColor Green
    }
}

# 4. Ensure data directory exists
if (-not (Test-Path "data")) {
    New-Item -ItemType Directory -Path "data" -Force | Out-Null
}

# 5. Start Django CRM Server in the background (output directly to console)
Write-Host "Starting Django CRM server..." -ForegroundColor Cyan
$pythonExe = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonExe)) {
    $pythonExe = "python"
}
$djangoProcess = Start-Process -FilePath $pythonExe -ArgumentList "-u", "manage.py", "runserver" -NoNewWindow -PassThru


# 6. Wait for Django server to initialize, then open the browser to the Dashboard
Write-Host "Opening Outreach Manager Dashboard at http://localhost:8000/..." -ForegroundColor Green
Start-Sleep -Seconds 3
Start-Process "http://localhost:8000/"

# 7. Block and wait for Django server to exit (Ctrl+C terminates)
try {
    $djangoProcess.WaitForExit()
} finally {
    Write-Host "`nStopping Django CRM server..." -ForegroundColor Yellow
    if ($djangoProcess -and -not $djangoProcess.HasExited) {
        Stop-Process -Id $djangoProcess.Id -Force -ErrorAction SilentlyContinue
    }
    Write-Host "Outreach Manager shut down successfully." -ForegroundColor Green
}
