<#
.SYNOPSIS
    Stop a capture that is stuck, crashed, or simply needs to end now.

.DESCRIPTION
    A crashed capture leaves three things behind, in descending order of how
    much they matter:

      1. Pro Tools still rolling, still record-armed. It will happily record
         over the next take, or fill a drive.
      2. The Fantom still sounding, because the note-offs never got sent.
      3. Orphaned processes: `fantom_stem.py run` and the `ptools.js serve`
         client it talks to.

    This stops all three, in that order. Pro Tools is stopped BEFORE the
    client is killed, because the client is how we talk to Pro Tools.

    Safe to run at any time, including when nothing is wrong.

.PARAMETER KeepProcesses
    Stop the transport and silence the synth, but leave the running capture
    alone. Use when a take is mid-flight and you only want the noise to stop.

.PARAMETER NoPanic
    Skip the MIDI panic. The Fantom keeps sounding whatever it was sounding.

.EXAMPLE
    .\Stop-Capture.ps1
#>
[CmdletBinding()]
param(
    [switch]$KeepProcesses,
    [switch]$NoPanic
)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ptools = Join-Path $root 'ptools.js'

function Say($msg) { Write-Host "  $msg" }

Write-Host ""
Write-Host "Stopping capture" -ForegroundColor Cyan
Write-Host ""

# --- 1. Pro Tools: stop the transport, then disarm -------------------------
# `stop` in ptools.js checks the transport state first, so calling it when
# already stopped does nothing rather than starting playback.
if (Test-Path $ptools) {
    try {
        $out = & node $ptools stop 2>&1 | Out-String
        if ($out -match 'TState_(\w+)') { Say "Pro Tools transport: $($Matches[1])" }
        else { Say "Pro Tools transport: stopped" }
    } catch {
        Say "Could not stop Pro Tools: $($_.Exception.Message)"
    }

    try {
        $out = & node $ptools disarm-all 2>&1 | Out-String
        if ($out -match '"disarmed":\s*(\d+)') { Say "Disarmed $($Matches[1]) track(s)" }
    } catch {
        Say "Could not disarm tracks: $($_.Exception.Message)"
    }
} else {
    Say "ptools.js not found beside this script - skipping Pro Tools"
}

# --- 2. Orphaned processes -------------------------------------------------
# Killed BEFORE the MIDI panic, not after: the running capture holds the
# Fantom's USB endpoint, and panic.py cannot open it while that process
# lives -- it fails with "Access denied", precisely when you need it most.
#
# Matched on COMMAND LINE, never on image name. Killing every node.exe would
# take out MCP servers, editors, and anything else that happens to be node.
if (-not $KeepProcesses) {
    $targets = Get-CimInstance Win32_Process -Filter "Name='node.exe' OR Name='python.exe'" |
        Where-Object { $_.CommandLine -match 'fantom_stem\.py|ptools\.js' }

    if (-not $targets) {
        Say "No capture processes running"
    }
    foreach ($p in $targets) {
        $what = if ($p.CommandLine -match 'fantom_stem') { 'capture' } else { 'PTSL client' }
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
            Say "Killed $what (pid $($p.ProcessId))"
        } catch {
            Say "Could not kill pid $($p.ProcessId): $($_.Exception.Message)"
        }
    }
    if ($targets) { Start-Sleep -Milliseconds 400 }   # let the USB handle close
}

# --- 3. The synth ----------------------------------------------------------
if (-not $NoPanic) {
    $panic = Join-Path $root 'panic.py'
    if (Test-Path $panic) {
        $py = if ($env:PYTHON) { $env:PYTHON } else { 'python' }
        try {
            $out = & $py $panic 2>&1 | Out-String
            if ($out -match 'note-offs sent') {
                Say "Fantom silenced"
            } else {
                Say "Panic: $(($out.Trim() -split "`n" | Select-Object -First 1))"
                if ($KeepProcesses) {
                    Say "  (-KeepProcesses leaves the capture holding the USB port, so panic cannot open it)"
                } else {
                    Say "  Silence it on the instrument: press STOP, or turn it down."
                }
            }
        } catch {
            Say "Could not silence the Fantom: $($_.Exception.Message)"
        }
    }
}

Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host ""
