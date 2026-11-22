<#
.SYNOPSIS
  Install Celius on this Windows PC for the current user (docs/remote/INSTALL.md).

.DESCRIPTION
  Creates a private Python environment in %LOCALAPPDATA%\CeliusAgent, puts the `celius`
  command on your PATH, signs this PC in (it shows a code; you accept the device in Celius,
  Remote machines), and adds a scheduled task that runs the agent when you sign in to
  Windows, so Celius can use this PC while you are away from it.

  It runs as you, not as a Windows service: it can use your own folders, its sign-in is
  encrypted with DPAPI for your account only, and it never holds administrator rights.

  Then open a new terminal and run `celius` in a project folder to chat on this PC.

  From your Celius server, with nothing else needed on the PC (PowerShell, not as admin):
    irm https://celius.example/install.ps1 | iex
    irm "https://celius.example/install.ps1?user=alice" | iex
  (`?upgrade=1` upgrades, `?uninstall=1` removes.) If Python is missing it is installed
  for your account with winget.

.EXAMPLE
  .\install-windows.ps1 -Server https://celius.example -CeliusUser alice
.EXAMPLE
  .\install-windows.ps1 -Upgrade
.EXAMPLE
  .\install-windows.ps1 -Uninstall
#>
[CmdletBinding(DefaultParameterSetName = "Install")]
param(
  [Parameter(ParameterSetName = "Install", Mandatory = $true)][string]$Server,
  # Your Celius user name: the device then waits in your list in Celius.
  [Parameter(ParameterSetName = "Install")][string]$CeliusUser,
  # A name for this PC in Celius (default: its computer name).
  [Parameter(ParameterSetName = "Install")][string]$Name,
  [Parameter(ParameterSetName = "Install")][switch]$AllowInsecureHttp,
  [Parameter(ParameterSetName = "Install")][string]$CaFile,
  # Install and sign in only; no scheduled task.
  [Parameter(ParameterSetName = "Install")][switch]$NoTask,
  [Parameter(ParameterSetName = "Upgrade", Mandatory = $true)][switch]$Upgrade,
  [Parameter(ParameterSetName = "Uninstall", Mandatory = $true)][switch]$Uninstall,
  # A Python 3.10+ to build the environment with (default: the py launcher, then python).
  [string]$Python,
  # What to install (a pip requirement); the server's script passes its own package.
  # Default: this copy of the repository.
  [string]$Package
)
$ErrorActionPreference = "Stop"
$TaskName = "Celius Agent"
$Base = Join-Path $env:LOCALAPPDATA "CeliusAgent"
$Venv = Join-Path $Base "venv"
$Scripts = Join-Path $Venv "Scripts"
if (-not $Package) {
  # Run from the repository: install the package next to this script.
  if (-not $PSScriptRoot) { throw "Run this from services\remote-agent, or: irm https://<celius>/install.ps1 | iex" }
  $Package = "$(Split-Path -Parent $PSScriptRoot)[monitor]"
}

function Set-UserPath([bool]$Present) {
  $current = [Environment]::GetEnvironmentVariable("Path", "User")
  $parts = @($current -split ";" | Where-Object { $_ -and ($_.TrimEnd("\") -ne $Scripts) })
  if ($Present) { $parts += $Scripts }
  [Environment]::SetEnvironmentVariable("Path", ($parts -join ";"), "User")
}

if ($Uninstall) {
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  Set-UserPath $false
  if (Test-Path $Venv) { Remove-Item -Recurse -Force $Venv }
  Write-Host "Removed. The device's data (sign-in, policy, state) is still in $Base;"
  Write-Host "remove the device in Celius and delete that folder to remove everything."
  return  # not exit: this may run inside the user's own PowerShell (irm | iex)
}

function Find-Python {
  if ($Python) { return , @($Python) }
  if (Get-Command py -ErrorAction SilentlyContinue) { return , @("py", "-3") }
  $found = Get-Command python -ErrorAction SilentlyContinue
  if ($found -and $found.Source -notlike "*WindowsApps*") { return , @($found.Source) }
  $local = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending | Select-Object -First 1
  if ($local) { return , @($local.FullName) }
  return $null
}

$py = Find-Python
if (-not $py -and (Get-Command winget -ErrorAction SilentlyContinue)) {
  Write-Host "Installing Python 3.12 for your account (winget)..."
  winget install --exact --id Python.Python.3.12 --scope user --silent `
    --accept-package-agreements --accept-source-agreements | Out-Host
  $py = Find-Python
}
if (-not $py) { throw "Python 3.10 or newer is needed: install it from python.org (tick 'Add to PATH'), then run this again." }
$pyExe = $py[0]
$pyArgs = if ($py.Length -gt 1) { $py[1..($py.Length - 1)] } else { @() }
& $pyExe @pyArgs -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) { throw "Python 3.10 or newer is required." }

New-Item -ItemType Directory -Force -Path $Base | Out-Null
if (-not (Test-Path (Join-Path $Scripts "python.exe"))) {
  Write-Host "Creating Celius's Python environment in $Venv"
  & $pyExe @pyArgs -m venv $Venv
}
$VenvPython = Join-Path $Scripts "python.exe"
$pipArgs = @("-m", "pip", "install", "--quiet", "--disable-pip-version-check")
if ($CaFile) { $pipArgs += @("--cert", $CaFile) }
& $VenvPython @pipArgs --upgrade pip
& $VenvPython @pipArgs --upgrade --force-reinstall --no-deps $Package
if ($LASTEXITCODE -ne 0) { throw "Installing Celius failed." }
& $VenvPython @pipArgs $Package
if ($LASTEXITCODE -ne 0) { throw "Installing Celius's dependencies failed." }
Set-UserPath $true
$env:Path = "$env:Path;$Scripts"

$hadTask = [bool](Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)
if (-not $Upgrade) {
  if (Test-Path (Join-Path $Base "credential")) {
    Write-Host "This PC is already signed in ($Base); keeping that sign-in."
  } else {
    $loginArgs = @("-m", "celius_agent", "login", "--server", $Server)
    if ($CeliusUser) { $loginArgs += @("--user", $CeliusUser) }
    if ($Name) { $loginArgs += @("--name", $Name) }
    if ($AllowInsecureHttp) { $loginArgs += "--allow-insecure-http" }
    if ($CaFile) { $loginArgs += @("--ca-file", $CaFile) }
    Write-Host ""
    & $VenvPython @loginArgs
    if ($LASTEXITCODE -ne 0) { throw "Sign-in did not finish; run the installer again." }
  }
}

if ((-not $Upgrade -and -not $NoTask) -or ($Upgrade -and $hadTask)) {
  # Start at sign-in, restart after failures, never time out, and keep running on battery.
  $pythonw = Join-Path $Scripts "pythonw.exe"
  $action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m celius_agent agent" -WorkingDirectory $Base
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
  $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew
  $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "Celius agent: lets Celius use this PC" -Force | Out-Null
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  Start-ScheduledTask -TaskName $TaskName
  Write-Host ""
  Write-Host "The agent is running (Task Scheduler: '$TaskName'; log: $Base\logs\agent.log)."
}

if (-not $Upgrade) {
  Write-Host ""
  Write-Host "Chat on this PC: open a new terminal and run 'celius' in a project folder"
  Write-Host "(it asks before working there)."
  Write-Host "To use this PC from Celius, list its folders in $Base\policy.json (`"roots`")"
  Write-Host "and set `"allow_shell`": true if it may run commands (applies to the next session)."
}
