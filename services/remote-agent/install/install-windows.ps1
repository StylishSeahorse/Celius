<#
.SYNOPSIS
  Install the Celius agent for the current Windows user (docs/remote/INSTALL.md).

.DESCRIPTION
  Creates a private Python environment in %LOCALAPPDATA%\CeliusAgent, registers this PC
  with Celius using a pairing code, and adds a scheduled task that starts the agent when
  you sign in and restarts it if it stops.

  It runs as you, not as a Windows service: it can then use your own folders and your
  Claude Code sign-in, its credential is encrypted with DPAPI for your account only, and it
  never holds administrator rights.

.EXAMPLE
  .\install-windows.ps1 -Server https://celius.example -Code ABCD-EFGH-JKLM
.EXAMPLE
  .\install-windows.ps1 -Upgrade
.EXAMPLE
  .\install-windows.ps1 -Uninstall
#>
[CmdletBinding(DefaultParameterSetName = "Install")]
param(
  [Parameter(ParameterSetName = "Install", Mandatory = $true)][string]$Server,
  [Parameter(ParameterSetName = "Install", Mandatory = $true)][string]$Code,
  [Parameter(ParameterSetName = "Install")][switch]$Claude,
  [Parameter(ParameterSetName = "Install")][switch]$AllowInsecureHttp,
  [Parameter(ParameterSetName = "Install")][string]$CaFile,
  [Parameter(ParameterSetName = "Upgrade", Mandatory = $true)][switch]$Upgrade,
  [Parameter(ParameterSetName = "Uninstall", Mandatory = $true)][switch]$Uninstall,
  # A Python 3.10+ to build the environment with (default: the py launcher, then python).
  [string]$Python
)
$ErrorActionPreference = "Stop"
$TaskName = "Celius Agent"
$Base = Join-Path $env:LOCALAPPDATA "CeliusAgent"
$Venv = Join-Path $Base "venv"
$Src = Split-Path -Parent $PSScriptRoot

if ($Uninstall) {
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  if (Test-Path $Venv) { Remove-Item -Recurse -Force $Venv }
  Write-Host "Removed. The agent's data (credential, policy, state) is still in $Base;"
  Write-Host "revoke the machine in Celius and delete that folder to remove everything."
  exit 0
}

function Find-Python {
  if ($Python) { return @($Python) }
  if (Get-Command py -ErrorAction SilentlyContinue) { return @("py", "-3") }
  $found = Get-Command python -ErrorAction SilentlyContinue
  if ($found -and $found.Source -notlike "*WindowsApps*") { return @($found.Source) }
  throw "Python 3.10 or newer is needed: install it from python.org (tick 'Add to PATH')."
}

$py = Find-Python
$pyExe = $py[0]
$pyArgs = if ($py.Length -gt 1) { $py[1..($py.Length - 1)] } else { @() }
& $pyExe @pyArgs -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) { throw "Python 3.10 or newer is required." }

New-Item -ItemType Directory -Force -Path $Base | Out-Null
if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) {
  Write-Host "Creating the agent's Python environment in $Venv"
  & $pyExe @pyArgs -m venv $Venv
}
$VenvPython = Join-Path $Venv "Scripts\python.exe"
& $VenvPython -m pip install --quiet --upgrade pip
$extras = if ($Claude) { "monitor,claude" } else { "monitor" }
& $VenvPython -m pip install --quiet "$Src[$extras]"
if ($LASTEXITCODE -ne 0) { throw "Installing the agent failed." }

if (-not $Upgrade) {
  $registerArgs = @("-m", "celius_agent", "register", "--server", $Server, "--code", $Code)
  if ($AllowInsecureHttp) { $registerArgs += "--allow-insecure-http" }
  if ($CaFile) { $registerArgs += @("--ca-file", $CaFile) }
  & $VenvPython @registerArgs
  if ($LASTEXITCODE -ne 0) { throw "Registration failed (is the pairing code still valid?)." }
}

# Start at sign-in, restart after failures, never time out, and keep running on battery.
$pythonw = Join-Path $Venv "Scripts\pythonw.exe"
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m celius_agent run" -WorkingDirectory $Base
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -StartWhenAvailable -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
  -Principal $principal -Description "Celius Remote Agent: runs Celius sessions on this PC" -Force | Out-Null
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName $TaskName

Write-Host ""
Write-Host "The agent is running (Task Scheduler: '$TaskName'; log: $Base\logs\agent.log)."
if (-not $Upgrade) {
  Write-Host "Now: list the folders Celius may use in $Base\policy.json (`"roots`"),"
  Write-Host "set `"allow_shell`": true if it may run commands, then restart the task:"
  Write-Host "  Stop-ScheduledTask '$TaskName'; Start-ScheduledTask '$TaskName'"
  Write-Host "and authorise the machine in Celius (Remote machines)."
}
