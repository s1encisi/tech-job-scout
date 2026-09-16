# This script only changes the local Windows Task Scheduler when -Register is supplied.
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][string]$Workspace,
    [Parameter(Mandatory = $true)][ValidatePattern('^([01][0-9]|2[0-3]):[0-5][0-9]$')][string]$At,
    [string]$TaskName = 'EnvAIJobScout',
    [switch]$Register
)
$ErrorActionPreference = 'Stop'
$python = (Get-Command python -ErrorAction Stop).Source
$runner = Join-Path $PSScriptRoot 'run_daily.py'
$root = [IO.Path]::GetFullPath($Workspace)
if ($root.Contains('"') -or $runner.Contains('"')) { throw 'Quotes in paths are not supported.' }
$arguments = '"' + $runner + '" --workspace "' + $root + '" --execute'
Write-Output "Task: $TaskName; Daily $At in this computer's local timezone; workspace: $root"
Write-Output "Action: $python $arguments"
Write-Output 'Requires a signed-in interactive Windows session, an available network and Codex login. No password is stored.'
if (-not $Register) { Write-Output 'Preview only. No scheduled task has been created.'; return }
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    throw 'The task already exists. This script will not overwrite it; review its settings first.'
}
if ($PSCmdlet.ShouldProcess($TaskName, 'Create daily local scheduled task')) {
    $action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -Daily -At ([datetime]::ParseExact($At, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture))
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Evidence-first environment + AI job search with Codex; no auto-application.'
}
