<#
.SYNOPSIS
  Installs the Fabric Desktop agent on a Windows server as a startup task that restarts on failure.

.DESCRIPTION
  Creates C:\ProgramData\FabricDesktop, installs the sidecar into a virtual environment under
  C:\Program Files\FabricDesktopAgent, and registers the Task Scheduler task \FabricDesktop\Agent.
  The task starts at boot with no one signed in, runs as the account you give (a gMSA such as
  CONTOSO\fd-agent$ is best: no password to manage), and restarts every minute if it stops.
  Run from an elevated PowerShell in the repository root. See docs/agent.md, "Windows".

.EXAMPLE
  .\deploy\windows\install-agent.ps1 -Account 'CONTOSO\fd-agent$'
#>
param(
    [Parameter(Mandatory = $true)] [string] $Account,
    [string] $Python = "py -3.12",
    [string] $InstallDir = "$env:ProgramFiles\FabricDesktopAgent",
    [string] $DataDir = "$env:ProgramData\FabricDesktop"
)
$ErrorActionPreference = "Stop"

New-Item -ItemType Directory -Force -Path $InstallDir, $DataDir, "$DataDir\agent" | Out-Null
Invoke-Expression "$Python -m venv `"$InstallDir\venv`""
& "$InstallDir\venv\Scripts\python.exe" -m pip install --upgrade pip
& "$InstallDir\venv\Scripts\python.exe" -m pip install "$PSScriptRoot\..\..\sidecar"

if (-not (Test-Path "$DataDir\agent.toml")) {
    Copy-Item "$PSScriptRoot\..\agent.example.toml" "$DataDir\agent.toml"
    Write-Host "Edit $DataDir\agent.toml, then run: $InstallDir\venv\Scripts\fabric-desktop.exe agent check"
}
# The agent's account may write its home (queue, logs); only admins may change the config.
icacls "$DataDir\agent" /grant "${Account}:(OI)(CI)M" | Out-Null

$exe = "$InstallDir\venv\Scripts\fabric-desktop.exe"
$action = New-ScheduledTaskAction -Execute $exe -Argument "agent run" -WorkingDirectory "$DataDir\agent"
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -MultipleInstances IgnoreNew -AllowStartIfOnBatteries
if ($Account.EndsWith('$')) {
    $principal = New-ScheduledTaskPrincipal -UserId $Account -LogonType Password   # gMSA: Windows fetches the password
} else {
    $principal = New-ScheduledTaskPrincipal -UserId $Account -LogonType S4U        # no stored password, no network shares
}
Register-ScheduledTask -TaskPath "\FabricDesktop\" -TaskName "Agent" -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "Registered \FabricDesktop\Agent. Start it now with: Start-ScheduledTask -TaskPath '\FabricDesktop\' -TaskName Agent"
