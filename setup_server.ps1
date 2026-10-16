<#
StakBridge SuperAdmin server - automated setup for a Windows VPS.

Run it through setup_server.bat (which elevates to Administrator), not
directly. Safe to re-run at any time: every step checks what is already
done, keeps existing settings unless you choose to change them, and finishes
by (re)starting the server.

What it does, in order:
  1. Finds a usable Python (3.10+, python.org install - not the Store stub)
  2. Creates .venv, or recreates it if a copied one is broken
  3. Installs requirements-server.txt + waitress (website, email, EXE builder)
  4. Creates the SuperAdmin login (if not set up yet)
  5. Asks for the public server address and email (SMTP) settings and saves
     them to server_settings.env (readable by Administrators/SYSTEM only)
  6. Opens the port in Windows Firewall
  7. Registers a Task Scheduler task that runs start_server.bat as SYSTEM at
     every boot, then starts it and checks the panel answers

Actions:
  -Action Setup    (default) everything above
  -Action Https    put the panel on your own domain with HTTPS (Caddy), e.g.
                   https://stakbridge.thinkstak.com - run Setup first
  -Action Restart  stop + start the server (after editing server_settings.env)
  -Action Stop     stop the server
#>

param(
    [ValidateSet("Setup", "Https", "Restart", "Stop")]
    [string]$Action = "Setup"
)

$ErrorActionPreference = "Continue"

# Windows PowerShell 5.1 can default to TLS 1.0, which most HTTPS sites
# (IP lookup, Caddy download, our own https check) now refuse.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir = Join-Path $Root ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$SettingsFile = Join-Path $Root "server_settings.env"
$LogFile = Join-Path $Root "logs\server.log"
$CredentialsFile = Join-Path $Root "superadmin_panel\superadmin_config.json"
$TaskName = "StakBridge SuperAdmin Server"
$FirewallRuleName = "StakBridge SuperAdmin Panel"
$CaddyDir = Join-Path $Root "caddy"
$CaddyExe = Join-Path $CaddyDir "caddy.exe"
$Caddyfile = Join-Path $CaddyDir "Caddyfile"
$CaddyLogFile = Join-Path $Root "logs\caddy.log"
$CaddyTaskName = "StakBridge HTTPS Proxy"
$CaddyFirewallRuleName = "StakBridge HTTPS Proxy"
$DefaultPort = 5070
# Ports reserved for other services on this VPS - never use them for the panel.
$BlockedPorts = @(5000, 5001, 8000, 8010)
# Ports web browsers refuse to open ("unsafe port"), so the panel and /trial would not load.
$BrowserBlockedPorts = @(1719, 1720, 1723, 2049, 3659, 4045, 5060, 5061, 6000, 6566, 6665, 6666, 6667, 6668, 6669, 6697, 10080)

Set-Location $Root

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

function Write-Step($text) {
    Write-Host ""
    Write-Host "==> $text" -ForegroundColor Cyan
}

function Write-Ok($text) { Write-Host "    OK: $text" -ForegroundColor Green }
function Write-Note($text) { Write-Host "    $text" -ForegroundColor Yellow }

function Fail($text) {
    Write-Host ""
    Write-Host "ERROR: $text" -ForegroundColor Red
    Write-Host ""
    exit 1
}

function Ask($prompt, $default) {
    $label = $prompt
    if ($default) { $label = "$prompt [$default]" }
    $value = Read-Host "    $label"
    if ([string]::IsNullOrWhiteSpace($value)) { return $default }
    return $value.Trim()
}

function Ask-YesNo($prompt, [bool]$defaultYes = $true) {
    $hint = "y/N"
    if ($defaultYes) { $hint = "Y/n" }
    while ($true) {
        $value = Read-Host "    $prompt ($hint)"
        if ([string]::IsNullOrWhiteSpace($value)) { return $defaultYes }
        switch -Regex ($value.Trim()) {
            "^(y|yes)$" { return $true }
            "^(n|no)$" { return $false }
        }
    }
}

function Ask-Secret($prompt) {
    $secure = Read-Host "    $prompt" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

function Read-Settings {
    $settings = [ordered]@{}
    if (Test-Path $SettingsFile) {
        foreach ($line in Get-Content -Path $SettingsFile -Encoding UTF8) {
            $trimmed = $line.Trim()
            if (-not $trimmed -or $trimmed.StartsWith("#")) { continue }
            $index = $trimmed.IndexOf("=")
            if ($index -lt 1) { continue }
            $settings[$trimmed.Substring(0, $index)] = $trimmed.Substring($index + 1)
        }
    }
    return $settings
}

function Write-Settings($settings) {
    $lines = @(
        "# StakBridge SuperAdmin server settings - written by setup_server.bat.",
        "# Change them by re-running setup_server.bat, or edit this file and then",
        "# run restart_server.bat. One KEY=value per line, no quotes."
    )
    foreach ($key in $settings.Keys) {
        if ($null -ne $settings[$key] -and "$($settings[$key])" -ne "") {
            $lines += "$key=$($settings[$key])"
        }
    }
    [IO.File]::WriteAllLines($SettingsFile, [string[]]$lines, (New-Object Text.UTF8Encoding $false))

    # Contains the mailbox password - Administrators and SYSTEM only.
    & icacls $SettingsFile /inheritance:r /grant:r "*S-1-5-32-544:F" "*S-1-5-18:F" | Out-Null
}

function Get-ServerPort {
    $settings = Read-Settings
    if ($settings["SERVER_PORT"]) { return [int]$settings["SERVER_PORT"] }
    return $DefaultPort
}

function Stop-PanelServer {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }

    # Stopping the task does not reliably end its child processes. Only THIS
    # panel's processes are ended: the server listening on this panel's port,
    # and the start_server.bat restart loop above it (ended first, else it
    # relaunches the server). Another product's panel on the same VPS (for
    # example the Forex CopyTrader one on port 5050) is never touched.
    $port = Get-ServerPort
    $all = @{}
    foreach ($process in Get-CimInstance Win32_Process) { $all[[int]$process.ProcessId] = $process }
    $chain = @()
    $listeners = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    foreach ($listener in $listeners) {
        $current = $all[[int]$listener.OwningProcess]
        for ($depth = 0; $current -and $depth -lt 5; $depth++) {
            $chain += $current
            if ($current.Name -eq "cmd.exe" -and $current.CommandLine -like "*start_server.bat*") { break }
            $current = $all[[int]$current.ParentProcessId]
        }
    }
    # The loop (cmd.exe) first, then the server under it.
    $ordered = @($chain | Where-Object { $_.Name -eq "cmd.exe" }) + @($chain | Where-Object { $_.Name -ne "cmd.exe" })
    foreach ($process in $ordered) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if ($ordered) { Start-Sleep -Seconds 2 }
}

function Get-PublicIp {
    try { return (Invoke-RestMethod -Uri "https://api.ipify.org" -TimeoutSec 10).ToString().Trim() }
    catch { return $null }
}

function Set-PanelServerAddress($url) {
    # The panel's Settings -> Server Address (stored in the database) wins over
    # PUBLIC_SERVER_URL, so keep it in step - otherwise new EXEs would still be
    # built with the old address.
    $env:STAKBRIDGE_NEW_SERVER_URL = $url
    $script = Join-Path $env:TEMP "stakbridge_set_server_url.py"
    @(
        "import os, sqlite3",
        "from superadmin_panel import db, repositories",
        "conn = sqlite3.connect(str(db.default_db_path()))",
        "if conn.execute(""SELECT name FROM sqlite_master WHERE name = 'settings'"").fetchone():",
        "    repositories.set_setting(conn, repositories.SETTING_PUBLIC_SERVER_URL, os.environ['STAKBRIDGE_NEW_SERVER_URL'])"
    ) | Set-Content -Path $script -Encoding ASCII
    # A script run from %TEMP% does not see the project folder on sys.path.
    $env:PYTHONPATH = $Root
    & $VenvPython $script
    $exitCode = $LASTEXITCODE
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    Remove-Item $script -ErrorAction SilentlyContinue
    Remove-Item Env:STAKBRIDGE_NEW_SERVER_URL -ErrorAction SilentlyContinue
    if ($exitCode -eq 0) { Write-Ok "Panel Settings -> Server Address set to $url" }
    else { Write-Note "Could not update Settings -> Server Address automatically - set it to $url in the panel." }
}

function Register-BootTask($name, $batchFile) {
    $taskAction = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c ""$batchFile""" -WorkingDirectory $Root
    $taskTrigger = New-ScheduledTaskTrigger -AtStartup
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    $taskSettings = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $name -Action $taskAction -Trigger $taskTrigger `
        -Principal $taskPrincipal -Settings $taskSettings -Force | Out-Null
}

function Stop-CaddyProxy {
    $task = Get-ScheduledTask -TaskName $CaddyTaskName -ErrorAction SilentlyContinue
    if ($task) { Stop-ScheduledTask -TaskName $CaddyTaskName -ErrorAction SilentlyContinue }
    # Only this folder's Caddy (caddy\caddy.exe here), and its start_caddy.bat loop.
    $processes = Get-CimInstance Win32_Process | Where-Object {
        ($_.Name -eq "caddy.exe" -and $_.ExecutablePath -eq $CaddyExe) -or
        ($_.Name -eq "cmd.exe" -and $_.CommandLine -like "*start_caddy.bat*")
    }
    foreach ($process in $processes) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if ($processes) { Start-Sleep -Seconds 2 }
}

function Start-PanelServer {
    $port = Get-ServerPort

    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) {
        $owner = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
        Fail "Port $port is already in use by '$($owner.ProcessName)' (PID $($listener.OwningProcess)). Free it, or change SERVER_PORT in server_settings.env, then run restart_server.bat."
    }

    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) { Fail "The '$TaskName' task is not registered. Run setup_server.bat first." }

    Start-ScheduledTask -TaskName $TaskName

    Write-Host "    Waiting for the panel to answer on port $port ..."
    for ($i = 0; $i -lt 45; $i++) {
        Start-Sleep -Seconds 1
        try {
            $response = Invoke-WebRequest -Uri "http://127.0.0.1:$port/login" -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -eq 200) {
                Write-Ok "Panel is running on port $port."
                return
            }
        } catch { }
    }

    Write-Host ""
    Write-Host "The panel did not answer within 45 seconds. Last lines of logs\server.log:" -ForegroundColor Red
    if (Test-Path $LogFile) { Get-Content $LogFile -Tail 25 }
    Fail "Server did not start - see the log above."
}

# --------------------------------------------------------------------------
# Stop / Restart
# --------------------------------------------------------------------------

if ($Action -eq "Stop") {
    Write-Step "Stopping the SuperAdmin server"
    Stop-PanelServer
    Write-Ok "Stopped. It will start again at the next reboot (or run restart_server.bat)."
    exit 0
}

if ($Action -eq "Restart") {
    Write-Step "Restarting the SuperAdmin server"
    Stop-PanelServer
    Start-PanelServer
    exit 0
}

# --------------------------------------------------------------------------
# Https - own domain + HTTPS certificate via Caddy
# --------------------------------------------------------------------------

if ($Action -eq "Https") {
    Write-Host "============================================================"
    Write-Host " StakBridge SuperAdmin - domain + HTTPS setup"
    Write-Host "============================================================"

    if (-not (Test-Path $VenvPython) -or -not (Test-Path $SettingsFile)) {
        Fail "Run setup_server.bat first - this step puts HTTPS in front of the panel it installs."
    }
    $settings = Read-Settings
    $port = Get-ServerPort

    # ---- 1. Domain ----
    Write-Step "Step 1/6 - Domain"
    $defaultDomain = $settings["PUBLIC_DOMAIN"]
    if (-not $defaultDomain) { $defaultDomain = "stakbridge.thinkstak.com" }
    while ($true) {
        $domain = (Ask "Domain for the panel" $defaultDomain).ToLower()
        $domain = $domain -replace "^https?://", "" -replace "/.*$", ""
        if ($domain -match "^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$") { break }
        Write-Note "Enter just the name, like stakbridge.thinkstak.com (no http://, no /path)."
    }

    # ---- 2. DNS ----
    Write-Step "Step 2/6 - Checking DNS for $domain"
    $publicIp = Get-PublicIp
    $resolved = @()
    try {
        $resolved = @(Resolve-DnsName -Name $domain -Type A -Server 8.8.8.8 -DnsOnly -ErrorAction Stop |
            Where-Object { $_.Type -eq "A" } | ForEach-Object { $_.IPAddress })
    } catch { }

    if (-not $publicIp) {
        Write-Note "Could not detect this server's public IP - skipping the DNS check."
    } elseif ($resolved -notcontains $publicIp) {
        $labels = $domain.Split(".")
        $hostLabel = "@"
        if ($labels.Count -gt 2) { $hostLabel = ($labels[0..($labels.Count - 3)] -join ".") }
        $rootDomain = ($labels[-2..-1] -join ".")
        $current = "nothing yet"
        if ($resolved.Count) { $current = $resolved -join ", " }
        Write-Host ""
        Write-Host "    $domain points to $current, not to this server ($publicIp)." -ForegroundColor Red
        Write-Host ""
        Write-Host "    Add this DNS record in Hostinger (hPanel -> Domains -> $rootDomain -> DNS / Nameservers):"
        Write-Host "        Type:       A"
        Write-Host "        Name:       $hostLabel"
        Write-Host "        Points to:  $publicIp"
        Write-Host "        TTL:        300"
        Write-Host ""
        Write-Host "    Wait 5-30 minutes, then run setup_https.bat again."
        exit 1
    } else {
        Write-Ok "$domain -> $publicIp"
    }

    # ---- 3. Ports 80/443 ----
    Write-Step "Step 3/6 - Checking ports 80 and 443"
    Stop-CaddyProxy
    foreach ($webPort in @(80, 443)) {
        $listener = Get-NetTCPConnection -LocalPort $webPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($listener) {
            $owner = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
            $ownerName = "$($owner.ProcessName) (PID $($listener.OwningProcess))"
            if ($listener.OwningProcess -eq 4) { $ownerName = "IIS or another Windows web service (System, PID 4)" }
            Fail "Port $webPort is already used by $ownerName. HTTPS needs ports 80 and 443 free."
        }
    }
    Write-Ok "Ports 80 and 443 are free"

    # ---- 4. Caddy ----
    Write-Step "Step 4/6 - Installing Caddy (gets and renews the HTTPS certificate)"
    New-Item -ItemType Directory -Force -Path $CaddyDir | Out-Null
    if (-not (Test-Path $CaddyExe)) {
        $download = "$CaddyExe.download"
        try {
            $ProgressPreference = "SilentlyContinue"
            Invoke-WebRequest -Uri "https://caddyserver.com/api/download?os=windows&arch=amd64" -OutFile $download -UseBasicParsing -TimeoutSec 300
            Move-Item -Force $download $CaddyExe
        } catch {
            Remove-Item $download -ErrorAction SilentlyContinue
            Fail "Downloading Caddy failed: $($_.Exception.Message)"
        }
    }
    $caddyVersion = & $CaddyExe version
    if ($LASTEXITCODE -ne 0) { Fail "caddy.exe is not working - delete the caddy folder and run this again." }
    Write-Ok "Caddy $caddyVersion"

    $storage = (Join-Path $CaddyDir "data") -replace "\\", "/"
    $caddyConfig = @"
# Written by setup_https.bat - re-run it to change the domain.
{
	storage file_system {
		root "$storage"
	}
}

$domain {
	reverse_proxy 127.0.0.1:$port
}
"@
    [IO.File]::WriteAllText($Caddyfile, $caddyConfig, (New-Object Text.UTF8Encoding $false))
    $validation = & $CaddyExe validate --config $Caddyfile --adapter caddyfile 2>&1
    if ($LASTEXITCODE -ne 0) {
        $validation | ForEach-Object { Write-Host "    $_" }
        Fail "The generated Caddyfile is invalid - see above."
    }
    Write-Ok "Caddyfile written: $domain -> 127.0.0.1:$port"

    # ---- 5. Firewall + settings ----
    Write-Step "Step 5/6 - Firewall and settings"
    Get-NetFirewallRule -DisplayName $CaddyFirewallRuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName $CaddyFirewallRuleName -Direction Inbound -Protocol TCP -LocalPort 80, 443 -Action Allow -Profile Any | Out-Null
    Write-Ok "Inbound TCP 80 and 443 allowed"

    Write-Note "Recommended: close direct access on port $port, so the panel is only reachable"
    Write-Note "at https://$domain (encrypted). EXEs already built with an http://IP:$port"
    Write-Note "address will stop connecting - rebuild and resend those."
    if (Ask-YesNo "Close direct access on port ${port}?" $true) {
        $settings["SERVER_BIND"] = "127.0.0.1"
        Get-NetFirewallRule -DisplayName $FirewallRuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
        Write-Ok "Port $port closed to the internet"
    } else {
        $settings["SERVER_BIND"] = "0.0.0.0"
    }

    $settings["PUBLIC_DOMAIN"] = $domain
    $settings["PUBLIC_SERVER_URL"] = "https://$domain"
    Write-Settings $settings
    Set-PanelServerAddress $settings["PUBLIC_SERVER_URL"]

    Write-Host ""
    Write-Host "    BEFORE CONTINUING: allow TCP 80 and 443 in your VPS provider's firewall panel." -ForegroundColor Yellow
    Write-Host "    The certificate cannot be issued until the internet can reach both ports." -ForegroundColor Yellow
    Read-Host "    Press Enter when done" | Out-Null

    # ---- 6. Start ----
    Write-Step "Step 6/6 - Starting the panel and HTTPS"
    Register-BootTask $CaddyTaskName "start_caddy.bat"
    Write-Ok "Task '$CaddyTaskName' registered"

    # Restart the panel so it picks up SERVER_BIND and the new address.
    Stop-PanelServer
    Start-PanelServer
    Start-ScheduledTask -TaskName $CaddyTaskName

    Write-Host "    Waiting for https://$domain (getting the certificate can take a minute) ..."
    $httpsOk = $false
    for ($i = 0; $i -lt 90; $i++) {
        Start-Sleep -Seconds 2
        try {
            $response = Invoke-WebRequest -Uri "https://$domain/login" -UseBasicParsing -TimeoutSec 5
            if ($response.StatusCode -eq 200) { $httpsOk = $true; break }
        } catch { }
    }
    if (-not $httpsOk) {
        Write-Host ""
        Write-Host "https://$domain did not answer within 3 minutes. Last lines of logs\caddy.log:" -ForegroundColor Red
        if (Test-Path $CaddyLogFile) { Get-Content $CaddyLogFile -Tail 25 }
        Fail "Usually: TCP 80/443 not allowed in the VPS provider's firewall, or DNS not updated yet. Fix it and run setup_https.bat again."
    }

    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host " HTTPS is live" -ForegroundColor Green
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "  Admin panel:        https://$domain"
    Write-Host "  Public trial page:  https://$domain/trial"
    Write-Host ""
    Write-Host "  Now, in the panel: rebuild the Trial EXE and every Admin's EXE"
    Write-Host "  (Build EXE) - each EXE keeps the address it was built with."
    Write-Host ""
    exit 0
}

# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------

Write-Host "============================================================"
Write-Host " StakBridge SuperAdmin - server setup"
Write-Host " Folder: $Root"
Write-Host "============================================================"

foreach ($required in @("requirements-server.txt", "desktop_launcher.py", "StakBridge.spec", "superadmin_panel\app.py")) {
    if (-not (Test-Path (Join-Path $Root $required))) {
        Fail "$required is missing. Copy the WHOLE project folder to this server, not just superadmin_panel."
    }
}

if ($Root -like "$env:USERPROFILE*") {
    Write-Note "This folder is inside your user profile (e.g. the Desktop). It works, but C:\StakBridgeServer"
    Write-Note "is safer - the server runs as SYSTEM and a profile folder can be moved or cleaned up."
    if (-not (Ask-YesNo "Continue from this folder anyway?" $true)) { exit 1 }
}

# A running server holds files inside .venv open - stop it before touching them.
Stop-PanelServer

# ---- 1. Python ------------------------------------------------------------

Write-Step "Step 1/7 - Finding Python"

$candidates = @()
if (Get-Command py -ErrorAction SilentlyContinue) { $candidates += , @("py", "-3") }
if (Get-Command python -ErrorAction SilentlyContinue) { $candidates += , @("python") }

$BasePython = $null
$BasePythonArgs = @()
foreach ($candidate in $candidates) {
    $exe = $candidate[0]
    $pyArgs = @($candidate | Select-Object -Skip 1)
    $output = & $exe @pyArgs -c "import sys; print(sys.version_info >= (3, 10)); print(sys.executable); print(sys.version.split()[0])" 2>$null
    if ($LASTEXITCODE -eq 0 -and $output -and $output[0] -eq "True") {
        if ($output[1] -like "*WindowsApps*") {
            Write-Note "Skipping Microsoft Store Python at $($output[1]) - it cannot run as a background service."
            continue
        }
        $BasePython = $exe
        $BasePythonArgs = $pyArgs
        Write-Ok "Python $($output[2]) at $($output[1])"
        break
    }
}
if (-not $BasePython) {
    Fail "No usable Python 3.10+ found. Install Python 3.12 from python.org, tick 'Add python.exe to PATH', then run this again."
}

# ---- 2. Virtual environment -----------------------------------------------

Write-Step "Step 2/7 - Preparing the virtual environment (.venv)"

if (Test-Path $VenvPython) {
    & $VenvPython -c "import sys" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Note "Existing .venv is broken (probably copied from another PC) - recreating it."
        Remove-Item -Recurse -Force $VenvDir
    }
} elseif (Test-Path $VenvDir) {
    Remove-Item -Recurse -Force $VenvDir
}

if (-not (Test-Path $VenvPython)) {
    & $BasePython @BasePythonArgs -m venv $VenvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $VenvPython)) { Fail "Could not create the virtual environment." }
    Write-Ok "Created .venv"
} else {
    Write-Ok "Existing .venv is fine"
}

# ---- 3. Dependencies ------------------------------------------------------

Write-Step "Step 3/7 - Installing dependencies (a few minutes the first time)"

& $VenvPython -m pip install --upgrade pip --disable-pip-version-check
& $VenvPython -m pip install -r (Join-Path $Root "requirements-server.txt") waitress --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Fail "Installing dependencies failed - see the pip output above." }
Write-Ok "Dependencies installed"

# ---- 4. SuperAdmin login --------------------------------------------------

Write-Step "Step 4/7 - SuperAdmin login"

$createLogin = $true
if (Test-Path $CredentialsFile) {
    $createLogin = -not (Ask-YesNo "A SuperAdmin login already exists. Keep it?" $true)
}

if ($createLogin) {
    $username = Ask "Username" "owner"
    while ($true) {
        $password = Ask-Secret "Password (min 8 characters)"
        if ($password.Length -lt 8) { Write-Note "Too short."; continue }
        $confirm = Ask-Secret "Type the password again"
        if ($password -ne $confirm) { Write-Note "Passwords do not match."; continue }
        break
    }
    # Passed via environment variables rather than the command line, so the
    # password never shows up in the process list.
    $env:SUPERADMIN_USERNAME = $username
    $env:SUPERADMIN_PASSWORD = $password
    & $VenvPython -m superadmin_panel.bootstrap_superadmin
    $exitCode = $LASTEXITCODE
    Remove-Item Env:SUPERADMIN_USERNAME, Env:SUPERADMIN_PASSWORD -ErrorAction SilentlyContinue
    $password = $null; $confirm = $null
    if ($exitCode -ne 0) { Fail "Could not save the SuperAdmin login." }
    Write-Ok "Login saved for '$username'"
} else {
    Write-Ok "Keeping the existing login"
}

# ---- 5. Server settings ---------------------------------------------------

Write-Step "Step 5/7 - Server settings"

$settings = Read-Settings

while ($true) {
    $portText = Ask "Port for the panel" $(if ($settings["SERVER_PORT"]) { $settings["SERVER_PORT"] } else { "$DefaultPort" })
    $port = 0
    if (-not [int]::TryParse($portText, [ref]$port) -or $port -lt 1024 -or $port -gt 65535) {
        Write-Note "Enter a number between 1024 and 65535."
        continue
    }
    if ($BlockedPorts -contains $port) {
        Write-Note "Port $port is reserved for other services on this server. Pick another."
        continue
    }
    if ($BrowserBlockedPorts -contains $port) {
        Write-Note "Port $port is blocked by web browsers (Chrome, Edge), so nobody could open the panel. Pick another."
        continue
    }
    break
}
$settings["SERVER_PORT"] = "$port"

$defaultUrl = $settings["PUBLIC_SERVER_URL"]
# Only refresh an IP-based default; never replace an https domain address.
$isIpAddress = $defaultUrl -match "^http://\d+\.\d+\.\d+\.\d+"
if (-not $defaultUrl -or ($isIpAddress -and $defaultUrl -notmatch ":$port/?$")) {
    $publicIp = Get-PublicIp
    if ($publicIp) { $defaultUrl = "http://${publicIp}:$port" }
    else { Write-Note "Could not detect this server's public IP automatically." }
}
Write-Note "This address is baked into every EXE you build - customers' apps call it."
while ($true) {
    $url = Ask "Public server address" $defaultUrl
    if ($url -match "^https?://[^/\s]+") { break }
    Write-Note "It must look like http://203.0.113.45:$port"
}
$settings["PUBLIC_SERVER_URL"] = $url.TrimEnd("/")

# email_defaults.env (shipped next to this script) fills in any email
# setting not yet saved, so pressing Enter keeps the default mailbox.
$EmailDefaultsFile = Join-Path $Root "email_defaults.env"
if (Test-Path $EmailDefaultsFile) {
    foreach ($line in Get-Content -Path $EmailDefaultsFile -Encoding UTF8) {
        if ($line -match "^\s*#" -or $line -notmatch "=") { continue }
        $key, $value = $line -split "=", 2
        $key = $key.Trim()
        if ($key -and -not $settings[$key]) { $settings[$key] = $value.Trim() }
    }
}

$configureEmail = $true
if ($settings["SMTP_HOST"]) {
    Write-Note "Email currently sends as $($settings['SMTP_USERNAME']) via $($settings['SMTP_HOST'])."
    $configureEmail = -not (Ask-YesNo "Keep these email settings?" $true)
} else {
    Write-Note "Email is needed for trial signups and expiry reminders."
    $configureEmail = Ask-YesNo "Set up email now?" $true
}

if ($configureEmail) {
    $settings["SMTP_HOST"] = Ask "SMTP server" $(if ($settings["SMTP_HOST"]) { $settings["SMTP_HOST"] } else { "smtp.hostinger.com" })
    $settings["SMTP_PORT"] = Ask "SMTP port (465 = SSL, 587 = STARTTLS)" $(if ($settings["SMTP_PORT"]) { $settings["SMTP_PORT"] } else { "465" })
    $defaultSecurity = "SSL"
    if ($settings["SMTP_PORT"] -eq "587") { $defaultSecurity = "STARTTLS" }
    $settings["SMTP_SECURITY"] = (Ask "Security (SSL or STARTTLS)" $defaultSecurity).ToUpper()
    $settings["SMTP_USERNAME"] = Ask "Mailbox address (sends the emails)" $settings["SMTP_USERNAME"]
    $smtpPassword = Ask-Secret "Mailbox password (leave blank to keep the current one)"
    if ($smtpPassword) { $settings["SMTP_PASSWORD"] = $smtpPassword }
    $settings["OWNER_NOTIFICATION_EMAIL"] = Ask "Your own email (gets alert copies)" $settings["OWNER_NOTIFICATION_EMAIL"]
}

if ($settings["SMTP_HOST"] -and $settings["SMTP_USERNAME"] -and $settings["SMTP_PASSWORD"]) {
    $settings["EXPIRY_REMINDER_ENABLED"] = "1"
} else {
    $settings["EXPIRY_REMINDER_ENABLED"] = "0"
    Write-Note "Email is not fully configured - trial and reminder emails will not be sent."
}

Write-Settings $settings
Write-Ok "Saved to server_settings.env"
Set-PanelServerAddress $settings["PUBLIC_SERVER_URL"]

# ---- 6. Firewall ----------------------------------------------------------

Write-Step "Step 6/7 - Windows Firewall"

Get-NetFirewallRule -DisplayName $FirewallRuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
if ($settings["SERVER_BIND"] -eq "127.0.0.1") {
    Write-Ok "Port $port stays closed - the panel is only reachable at $($settings['PUBLIC_SERVER_URL'])"
} else {
    New-NetFirewallRule -DisplayName $FirewallRuleName -Direction Inbound -Protocol TCP -LocalPort $port -Action Allow -Profile Any | Out-Null
    Write-Ok "Inbound TCP $port allowed"
    Write-Note "Also allow TCP $port in your VPS provider's firewall panel, or nothing outside can connect."
}

# ---- 7. Background service ------------------------------------------------

Write-Step "Step 7/7 - Run at every boot (Task Scheduler)"

Register-BootTask $TaskName "start_server.bat"
Write-Ok "Task '$TaskName' registered"

Start-PanelServer

# ---- Done -----------------------------------------------------------------

Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host " Setup complete" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Admin panel:        $($settings['PUBLIC_SERVER_URL'])"
Write-Host "  Public trial page:  $($settings['PUBLIC_SERVER_URL'])/trial"
Write-Host "  Server log:         $LogFile"
Write-Host ""
Write-Host "  Next, in the panel:"
Write-Host "   1. Settings -> Server Address must show $($settings['PUBLIC_SERVER_URL'])"
Write-Host "      (a value saved there overrides this setup)."
Write-Host "   2. Settings -> Free Trial Signups: the trial EXE builds by itself (a few minutes)."
Write-Host "   3. Using IIS for HTTPS? C:\inetpub\stakbridge\web.config must forward to 127.0.0.1:$port"
Write-Host "      (a different port there gives a 502 error) - then run iisreset."
Write-Host ""
Write-Host "  restart_server.bat  - restart after editing server_settings.env"
Write-Host "  stop_server.bat     - stop the server"
Write-Host "  setup_https.bat     - put the panel on your own domain with HTTPS"
Write-Host "  setup_server.bat    - re-run any time to change settings"
Write-Host ""
