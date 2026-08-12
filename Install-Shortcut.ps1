<#
.SYNOPSIS
    Put a Fantom Stem Capture shortcut on the desktop.

.DESCRIPTION
    Launches the console in Windows Terminal when available (it handles the
    24-bit colour the PHOSPHOR theme needs) and falls back to the plain
    PowerShell host otherwise.

.EXAMPLE
    .\Install-Shortcut.ps1
    .\Install-Shortcut.ps1 -Song mysong.mid
#>

param(
    [string]$Song = "",
    [string]$Name = "Fantom Stem Capture"
)

$ErrorActionPreference = "Stop"

$root    = Split-Path -Parent $MyInvocation.MyCommand.Path
$script  = Join-Path $root "Fantom-Capture.ps1"
$desktop = [Environment]::GetFolderPath("Desktop")
$lnk     = Join-Path $desktop "$Name.lnk"

if (-not (Test-Path $script)) { throw "Not found: $script" }

$inner = "-NoProfile -ExecutionPolicy Bypass -NoExit -File `"$script`""
if ($Song) { $inner += " -Song `"$Song`"" }

$wt = Get-Command wt.exe -ErrorAction SilentlyContinue
if ($wt) {
    $target = $wt.Source
    $args   = "--title `"FANTOM STEM CAPTURE`" --size 100,34 powershell.exe $inner"
} else {
    $target = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $args   = $inner
}

$shell = New-Object -ComObject WScript.Shell
$s = $shell.CreateShortcut($lnk)
$s.TargetPath       = $target
$s.Arguments        = $args
$s.WorkingDirectory = $root
$s.Description      = "Capture Fantom-G parts as isolated stems into Pro Tools"
$s.WindowStyle      = 1

# a keyboard-ish icon from the shell library, rather than a generic console
$icon = Join-Path $env:SystemRoot "System32\imageres.dll"
if (Test-Path $icon) { $s.IconLocation = "$icon,109" }
$s.Save()

Write-Host ""
Write-Host "  Created: $lnk"
Write-Host "  Target : $target"
Write-Host "  Host   : $(if ($wt) { 'Windows Terminal (100x34)' } else { 'PowerShell console' })"
if ($Song) { Write-Host "  Song   : $Song" }
Write-Host ""

