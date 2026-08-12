<#
.SYNOPSIS
    Console front end for the Fantom stem-capture rig.

.DESCRIPTION
    One screen over three moving parts: the Fantom's raw-USB MIDI link, the song
    being captured, and the Pro Tools session receiving the audio. Drives a full
    pass without touching a transport button.

    Three themes render the same state. Cycle with F8 (or T) at any time,
    including mid-capture:

      TURBO     Borland Turbo Vision, 1990. Every control one keystroke away.
      PHOSPHOR  Amber CRT. Big numbers, readable across a room mid-pass.
      ANSI      16-colour BBS art. Colour-coded columns for the results table.

.EXAMPLE
    .\Fantom-Capture.ps1
    .\Fantom-Capture.ps1 -Song mysong.mid -Theme phosphor
    .\Fantom-Capture.ps1 -Demo          # render all three and exit
#>

[CmdletBinding()]
param(
    [string]$Song  = "",
    [ValidateSet("turbo", "phosphor", "ansi")]
    [string]$Theme = "turbo",
    [switch]$Demo
)

$ErrorActionPreference = "Stop"

$script:Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:Stem = Join-Path $script:Root "fantom_stem.py"

# ptools.js sits beside this script by default; PTOOLS_JS overrides it.
$script:PTools = $env:PTOOLS_JS
if (-not $script:PTools) { $script:PTools = Join-Path $script:Root "ptools.js" }

# Find Python: PYTHON env var, then PATH, then the usual per-user install.
$script:Python = $env:PYTHON
if (-not $script:Python -or -not (Test-Path $script:Python)) {
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) { $script:Python = $cmd.Source }
}
if (-not $script:Python -or -not (Test-Path $script:Python)) {
    $guess = Get-ChildItem (Join-Path $env:LOCALAPPDATA "Programs\Python") -Filter python.exe `
             -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($guess) { $script:Python = $guess.FullName }
}
if (-not $script:Python) {
    Write-Host "Python not found. Set the PYTHON environment variable or add it to PATH."
    exit 1
}

# ============================================================== primitives ==

$E = [char]27
$RESET = "$E[0m"

# Repeat a character. PowerShell can multiply a string but not a char, which is
# an easy and very visible mistake -- "$([char]0x2550) * 3" prints the code point.
function Rep { param([int]$Code, [int]$N)
    if ($N -le 0) { return "" }
    return ("$([char]$Code)" * $N)
}

# Visible length, ignoring SGR escapes. Padding must measure this, not the raw
# string, or every coloured field silently loses characters off its right edge.
function Vis { param([string]$S)
    if ($null -eq $S) { return "" }
    return ($S -replace "$([char]27)\[[0-9;]*m", "")
}

function FitV { param([string]$S, [int]$W)
    if ($null -eq $S) { $S = "" }
    $len = (Vis $S).Length
    if ($len -ge $W) { return $S }
    return $S + (" " * ($W - $len))
}

function Plain { param([string]$S, [int]$W)
    if ($null -eq $S) { $S = "" }
    if ($S.Length -gt $W) { return $S.Substring(0, $W) }
    return $S.PadRight($W)
}

# IBM VGA text-mode palette as SGR codes
$FG = @{ black=30; blue=34; green=32; cyan=36; red=31; magenta=35; brown=33; lgray=37
         dgray=90; lblue=94; lgreen=92; lcyan=96; lred=91; lmagenta=95; yellow=93; white=97 }
$BG = @{ black=40; blue=44; green=42; cyan=46; red=41; magenta=45; brown=43; lgray=47
         dgray=100; lcyan=106; white=107 }

function C  { param([string]$N) "$E[$($FG[$N])m" }
function CB { param([string]$F,[string]$B) "$E[$($FG[$F]);$($BG[$B])m" }
function RGB { param([int]$R,[int]$G,[int]$B) "$E[38;2;$R;$G;${B}m" }

function Line { param([string]$Text = "") [Console]::WriteLine("$Text$RESET$E[K") }

function Enable-VirtualTerminal {
    if (-not ("Native.VT" -as [type])) {
        try {
            Add-Type -Namespace Native -Name VT -MemberDefinition @'
[DllImport("kernel32.dll", SetLastError=true)]
public static extern IntPtr GetStdHandle(int nStdHandle);
[DllImport("kernel32.dll", SetLastError=true)]
public static extern bool GetConsoleMode(IntPtr hConsoleHandle, out uint lpMode);
[DllImport("kernel32.dll", SetLastError=true)]
public static extern bool SetConsoleMode(IntPtr hConsoleHandle, uint dwMode);
'@
        } catch { }
    }
    try {
        $h = [Native.VT]::GetStdHandle(-11)
        $m = 0
        [void][Native.VT]::GetConsoleMode($h, [ref]$m)
        [void][Native.VT]::SetConsoleMode($h, $m -bor 0x0004)
    } catch { }
}

function Hide-Cursor  { [Console]::Write("$E[?25l") }
function Show-Cursor  { [Console]::Write("$E[?25h") }
function Clear-Screen { [Console]::Write("$E[2J$E[H") }
function Home         { [Console]::Write("$E[H") }

# =================================================================== state ==

$script:S = @{
    Theme = $Theme; SongFile = $Song; SongName = "(none loaded)"
    Bpm = 0.0; Bars = 0.0; TimeSig = "4/4"; Parts = @()
    Loops = 2; Gap = 2; Region = ""; Clock = $false
    PerTrack = $true; Tail = 4.0; CurTrack = ""
    DeviceOk = $false; UsbId = "-"; Endpoint = "-"
    PtSession = "not connected"; PtRate = "-"; PtTrack = "Fantom Stems"
    PtArmed = $false; PtOk = $false
    Phase = "idle"; CurPart = 0; CurName = ""; CurCh = 0; PassPct = 0
    Elapsed = "0:00.00"; Total = "0:00.00"; Sent = 0
    MeanLate = 0.0; WorstLate = 0.0; Markers = 0
    Message = ""; Cues = @()
}

$THEMES = @("turbo", "phosphor", "ansi")
$W = 78

# ============================================================== collectors ==

function Update-Device {
    try {
        $py = @"
import sys
sys.path.insert(0, r'$($script:Root)')
try:
    from usb_midi import RolandUsbMidiOut
    p = RolandUsbMidiOut()
    print('OK|%04X:%04X|if%d ep0x%02X' % (p.dev.idVendor, p.dev.idProduct,
          p.intf.bInterfaceNumber, p.ep.bEndpointAddress))
    p.close()
except Exception as e:
    print('ERR|offline|-')
"@
        $out = & $script:Python -c $py 2>$null
        $bits = ("$out".Trim() -split '\|')
        if ($bits.Count -ge 3 -and $bits[0] -eq "OK") {
            $script:S.DeviceOk = $true; $script:S.UsbId = $bits[1]; $script:S.Endpoint = $bits[2]
        } else {
            $script:S.DeviceOk = $false; $script:S.UsbId = "offline"; $script:S.Endpoint = "-"
        }
    } catch {
        $script:S.DeviceOk = $false; $script:S.UsbId = "error"; $script:S.Endpoint = "-"
    }
}

function Update-ProTools {
    try {
        $raw = (& node $script:PTools info 2>$null | Out-String)
        $j = $raw | ConvertFrom-Json
        $script:S.PtOk = $true
        $script:S.PtRate = ("$($j.sample_rate.sample_rate)" -replace '^SR_', '') + " Hz"
        $script:S.PtSession = "connected"
        $tr = (& node $script:PTools tracks 2>$null | Out-String)
        $script:S.PtArmed = ($tr -match '"is_record_enabled":true')
    } catch {
        $script:S.PtOk = $false; $script:S.PtSession = "not connected"; $script:S.PtRate = "-"
    }
}

function Update-Song {
    if (-not $script:S.SongFile) { return }
    $path = Join-Path $script:Root $script:S.SongFile
    if (-not (Test-Path $path)) { $script:S.Message = "not found: $($script:S.SongFile)"; return }
    $out = (& $script:Python $script:Stem inspect $path 2>$null | Out-String)
    $script:S.SongName = [IO.Path]::GetFileNameWithoutExtension($script:S.SongFile)
    if ($out -match 'Tempo:\s+([\d.]+) BPM')   { $script:S.Bpm     = [double]$Matches[1] }
    if ($out -match 'Length:\s+([\d.]+) bars') { $script:S.Bars    = [double]$Matches[1] }
    if ($out -match 'Time sig:\s+(\d+/\d+)')   { $script:S.TimeSig = $Matches[1] }
    $parts = @()
    foreach ($ln in ($out -split "`n")) {
        if ($ln -match '^\s+(\d+)\s\s(.{1,24}?)\s{2,}(\d+)\s+(\d+)\s+([\d.]+)') {
            $parts += ,@{ N=[int]$Matches[1]; Name=$Matches[2].Trim(); Ch=[int]$Matches[3]
                          Notes=[int]$Matches[4]; Bars=[double]$Matches[5] }
        }
    }
    $script:S.Parts = $parts
}

function Get-EstimatedPass {
    if ($script:S.Parts.Count -eq 0 -or $script:S.Bpm -le 0) { return "--:--" }
    $barSec = 240.0 / $script:S.Bpm
    $loopBars = [math]::Floor($script:S.Bars)
    if ($loopBars -lt 1) { $loopBars = 1 }
    if ($script:S.PerTrack) {
        # per part: the loops, the tail, plus ~2.5 s of Pro Tools handshaking
        $per = ($loopBars * $script:S.Loops * $barSec) + $script:S.Tail + 2.5
    } else {
        $per = (($loopBars * $script:S.Loops) + $script:S.Gap) * $barSec
    }
    $tot = $per * $script:S.Parts.Count
    return ("{0}:{1:00}" -f [int]($tot / 60), [int]($tot % 60))
}

# =========================================================== THEME: TURBO ===

function TvFrame { param([string]$Title)
    $inner = $W - 4
    $t = " $Title "
    $pad = $inner - $t.Length
    if ($pad -lt 2) { $pad = 2 }
    $l = [math]::Floor($pad / 2); $r = $pad - $l
    Line ((CB 'lcyan' 'blue') + "  " + [char]0x2554 + (Rep 0x2550 $l) + (C 'yellow') + $t +
          (C 'lcyan') + (Rep 0x2550 $r) + [char]0x2557)
}
function TvRow { param([string]$A, [string]$B)
    $body = (FitV $A 36) + (FitV $B 36)
    Line ((CB 'lcyan' 'blue') + "  " + [char]0x2551 + (C 'lgray') + (FitV $body ($W - 4)) +
          (C 'lcyan') + [char]0x2551)
}
function TvClose {
    Line ((CB 'lcyan' 'blue') + "  " + [char]0x255A + (Rep 0x2550 ($W - 4)) + [char]0x255D)
}
function TvBlank { Line ((CB 'lgray' 'blue') + (" " * $W)) }

function Render-Turbo {
    Line ((CB 'black' 'lgray') + (Plain "  File   Device   Song   Capture   Options   Help" $W))
    TvBlank

    $dev = (C 'white') + "Fantom G  "
    if ($script:S.DeviceOk) { $dev += (C 'lgreen') + "ONLINE" } else { $dev += (C 'lred') + "OFFLINE" }
    $trk = (C 'white') + $script:S.PtTrack + "  "
    if ($script:S.PtArmed) { $trk += (C 'lgreen') + "ARMED" } else { $trk += (C 'yellow') + "not armed" }

    TvFrame "Hardware & Session"
    TvRow ((C 'lcyan') + "Device     " + $dev) ((C 'lcyan') + "Session    " + (C 'white') + $script:S.PtSession)
    TvRow ((C 'lcyan') + "USB        " + (C 'white') + "$($script:S.UsbId) $($script:S.Endpoint)") `
          ((C 'lcyan') + "Rate       " + (C 'white') + $script:S.PtRate)
    TvRow ((C 'lcyan') + "Transport  " + (C 'white') + "raw bulk (WinUSB)") ((C 'lcyan') + "Track      " + $trk)
    TvClose
    TvBlank

    $region = $script:S.Region
    if (-not $region) { $region = "whole song" }
    TvFrame $script:S.SongName
    TvRow ((C 'lcyan') + "Tempo      " + (C 'white') + "$($script:S.Bpm) BPM  $($script:S.TimeSig)") `
          ((C 'lcyan') + "Loops      " + (C 'white') + "$($script:S.Loops)   gap $($script:S.Gap) bars")
    TvRow ((C 'lcyan') + "Length     " + (C 'white') + "$($script:S.Bars) bars") `
          ((C 'lcyan') + "Parts      " + (C 'white') + "$($script:S.Parts.Count)")
    TvRow ((C 'lcyan') + "Region     " + (C 'white') + $region) `
          ((C 'lcyan') + "Est. pass  " + (C 'yellow') + (Get-EstimatedPass))
    $mode = "one track per part"
    if (-not $script:S.PerTrack) { $mode = "single continuous pass" }
    $clk = "off"
    if ($script:S.Clock) { $clk = "24 PPQN" }
    TvRow ((C 'lcyan') + "Mode       " + (C 'white') + $mode) `
          ((C 'lcyan') + "Tail/Clock " + (C 'white') + ("{0:0.0}s / {1}" -f $script:S.Tail, $clk))
    TvClose
    TvBlank

    Line ((CB 'black' 'lgray') + (Plain "   #   PART                  CH   NOTES   BARS   STATUS" $W))
    $n = 0
    foreach ($p in $script:S.Parts) {
        if ($n -ge 8) { break }
        $row = "  " + ("{0:00}" -f $p.N) + "   " + (Plain $p.Name 20) + "  " + ("{0,2}" -f $p.Ch) +
               "  " + ("{0,6}" -f $p.Notes) + "   " + ("{0,4:0.0}" -f $p.Bars) + "   ready"
        if ($n -eq 0 -and $script:S.Phase -eq "idle") {
            Line ((CB 'black' 'cyan') + (Plain $row $W))
        } else {
            Line ((CB 'lgray' 'blue') + (Plain $row $W))
        }
        $n++
    }
    if ($script:S.Parts.Count -eq 0) {
        Line ((CB 'dgray' 'blue') + (Plain "        no song loaded - press F2" $W))
    } elseif ($script:S.Parts.Count -gt 8) {
        Line ((CB 'dgray' 'blue') + (Plain ("        ...$($script:S.Parts.Count - 8) more") $W))
    }

    TvBlank
    if ($script:S.Message) {
        Line ((CB 'yellow' 'blue') + (Plain ("  " + $script:S.Message) $W))
    }
    Line ((CB 'black' 'lgray') +
          (Plain "  F1 Help  F2 Load  F3 Region  F5 Test  F6 Arm  F8 Theme  F9 CAPTURE  F10 Quit" $W))
}

# ======================================================== THEME: PHOSPHOR ===

function Render-Phosphor {
    $A  = RGB 255 176 0
    $Ad = RGB 138 96 0
    $Ah = RGB 255 224 138

    Line ""
    Line ($A + "  FANTOM STEM CAPTURE   " + $Ad + "v1.0" + "        " + $Ad + $script:S.SongName)
    Line ($Ad + "  " + (Rep 0x2500 72))
    Line ""

    if ($script:S.Phase -eq "running") {
        Line ($Ad + "  CAPTURING PART $($script:S.CurPart) OF $($script:S.Parts.Count)")
        Line ($Ah + "  $($script:S.CurName)   " + $Ad + "ch$($script:S.CurCh)")
        if ($script:S.CurTrack) { Line ($Ad + "  -> " + $Ah + $script:S.CurTrack) }
    } elseif ($script:S.Phase -eq "done") {
        Line ($Ad + "  PASS COMPLETE")
        Line ($Ah + "  $($script:S.Parts.Count) PARTS   " + $Ad + "$($script:S.Markers) MARKERS")
    } else {
        Line ($Ad + "  READY")
        Line ($Ah + "  $($script:S.Bpm) BPM   " + $Ad + "$($script:S.Bars) BARS   $($script:S.Parts.Count) PARTS")
    }
    Line ""

    $full = 40
    $done = [int]([math]::Round($full * ($script:S.PassPct / 100.0)))
    if ($done -lt 0) { $done = 0 }
    if ($done -gt $full) { $done = $full }
    Line ($Ad + "  PASS   " + $A + (Rep 0x2588 $done) + $Ad + (Rep 0x2591 ($full - $done)) +
          "  " + $Ah + ("{0,3}" -f $script:S.PassPct) + "%   " + $Ad +
          "$($script:S.Elapsed) / $($script:S.Total)")
    Line ""

    $devTxt = "offline"
    if ($script:S.DeviceOk) { $devTxt = "$($script:S.UsbId) online" }
    $armTxt = "not armed"
    if ($script:S.PtArmed) { $armTxt = "armed" }
    $clkTxt = "off"
    if ($script:S.Clock) { $clkTxt = "24 PPQN locked" }

    # Every -f expression needs its own parentheses: PowerShell binds the comma
    # tighter than the format operator, so an unwrapped pair collapses into one
    # string and the second column vanishes.
    $rows = @(
        @("DEVICE",     $devTxt),
        @("SESSION",    $script:S.PtSession),
        @("TRACK",      $armTxt),
        @("RATE",       $script:S.PtRate),
        @("LOOPS",      "$($script:S.Loops)  gap $($script:S.Gap) bars"),
        @("CLOCK",      $clkTxt),
        @("MESSAGES",   ("{0:N0}" -f $script:S.Sent)),
        @("MARKERS",    "$($script:S.Markers) placed"),
        @("MEAN LATE",  ("{0:0.000} ms" -f $script:S.MeanLate)),
        @("WORST LATE", ("{0:0.000} ms" -f $script:S.WorstLate))
    )
    for ($i = 0; $i -lt $rows.Count; $i += 2) {
        $l = $Ad + "  " + (Plain $rows[$i][0] 12) + $A + (Plain $rows[$i][1] 24)
        if ($i + 1 -lt $rows.Count) {
            $l += $Ad + (Plain $rows[$i+1][0] 12) + $A + $rows[$i+1][1]
        }
        Line $l
    }

    Line ""
    Line ($Ad + "  " + (Rep 0x2500 72))
    Line ($Ad + "  F2 LOAD   F6 ARM   F8 THEME   F9 CAPTURE   ESC ABORT   F10 QUIT")
    if ($script:S.Message) { Line ($Ah + "  $($script:S.Message)") }
}

# ============================================================ THEME: ANSI ===

function Render-Ansi {
    Line ""
    Line ((C 'lmagenta') + "  " + (Rep 0x2584 22) + "   " + (C 'lcyan') + (Rep 0x2584 16))
    Line ((C 'magenta')  + "  FANTOM STEM CAPTURE       " + (C 'cyan') + "RESULTS")
    Line ((C 'dgray') + "  " + (Rep 0x2500 72))

    $state = (CB 'black' 'lgray') + " READY " + $RESET
    if ($script:S.Phase -eq "running") { $state = (CB 'black' 'cyan') + " CAPTURING " + $RESET }
    if ($script:S.Phase -eq "done")    { $state = (CB 'black' 'lcyan') + " PASS COMPLETE " + $RESET }
    $dot = [char]0x00B7
    Line ("  " + $state + " " + (C 'dgray') + $dot + " " + (C 'white') + $script:S.SongName +
          " " + (C 'dgray') + $dot + " " + (C 'yellow') + "$($script:S.Parts.Count) parts" +
          " " + (C 'dgray') + $dot + " " + (C 'yellow') + $script:S.Total +
          " " + (C 'dgray') + $dot + " " + (C 'lgreen') + "$($script:S.Markers) markers")
    Line ((C 'dgray') + "  " + (Rep 0x2500 72))
    Line ((CB 'black' 'lcyan') + (Plain "  #   PART                  CH   KEEP AT     NOTES   LEVEL        MARK" $W))

    $i = 0
    foreach ($p in $script:S.Parts) {
        if ($i -ge 10) { break }
        $keep = "-"
        $mark = (C 'dgray') + $dot
        if ($script:S.Cues.Count -gt $i) {
            $keep = $script:S.Cues[$i]
            $mark = (C 'lgreen') + [char]0x221A
        }
        $mn = [int][math]::Floor($p.Notes / 20)
        if ($mn -lt 1)  { $mn = 1 }
        if ($mn -gt 10) { $mn = 10 }
        $meter = (C 'lgreen') + (Rep 0x2588 $mn) + (C 'dgray') + (Rep 0x2591 (10 - $mn))
        Line ("  " + (C 'dgray') + ("{0:00}" -f $p.N) + "  " + (C 'white') + (Plain $p.Name 20) +
              " " + (C 'lblue') + ("{0,2}" -f $p.Ch) + "   " + (C 'yellow') + (Plain $keep 10) +
              " " + (C 'dgray') + ("{0,5}" -f $p.Notes) + "   " + $meter + "   " + $mark)
        $i++
    }
    if ($script:S.Parts.Count -eq 0) {
        Line ((C 'dgray') + "       no song loaded " + [char]0x2014 + " press F2")
    } elseif ($script:S.Parts.Count -gt 10) {
        Line ((C 'dgray') + "  ..  $($script:S.Parts.Count - 10) more parts")
    }

    $clkTxt = "off"
    if ($script:S.Clock) { $clkTxt = "24 PPQN locked" }
    Line ((C 'dgray') + "  " + (Rep 0x2500 72))
    Line ((C 'lcyan') + "  TIMING  " + (C 'lgray') +
          ("mean {0:0.000} ms " -f $script:S.MeanLate) + $dot +
          (" worst {0:0.000} ms" -f $script:S.WorstLate) + "      " +
          (C 'lcyan') + "CLOCK  " + (C 'lgray') + $clkTxt)
    Line ((C 'lcyan') + "  AUDIO   " + (C 'lgray') + "$($script:S.PtRate) " + $dot +
          " $($script:S.PtTrack) " + $dot + " $($script:S.PtSession)")
    if ($script:S.Message) { Line ((C 'brown') + "  NOTE    $($script:S.Message)") }
    Line ((C 'dgray') + "  " + (Rep 0x2500 72))
    Line ("  " + (C 'lgreen') + "[F2]" + (C 'lgray') + " load  " + (C 'lgreen') + "[F6]" + (C 'lgray') +
          " arm  " + (C 'lgreen') + "[F8]" + (C 'lgray') + " theme  " + (C 'lgreen') + "[F9]" +
          (C 'lgray') + " capture  " + (C 'lgreen') + "[F10]" + (C 'lgray') + " quit")
}

# ================================================================= render ===

function Render {
    Home
    switch ($script:S.Theme) {
        "turbo"    { Render-Turbo }
        "phosphor" { Render-Phosphor }
        "ansi"     { Render-Ansi }
    }
    [Console]::Write("$RESET$E[J")
}

function Switch-Theme {
    $i = [array]::IndexOf($THEMES, $script:S.Theme)
    $script:S.Theme = $THEMES[($i + 1) % $THEMES.Count]
    Clear-Screen
}

# ================================================================ capture ===

function Start-Capture {
    if (-not $script:S.SongFile) { $script:S.Message = "Load a song first (F2)"; return }
    $script:S.Phase = "running"; $script:S.PassPct = 0; $script:S.Cues = @(); $script:S.Message = ""
    Clear-Screen

    $a = @($script:Stem, "run", (Join-Path $script:Root $script:S.SongFile),
           "--usb", "--loops", $script:S.Loops, "--gap", $script:S.Gap,
           "--protools", "--pt-track", $script:S.PtTrack, "--yes")
    if ($script:S.PerTrack) { $a += @("--per-track", "--tail", $script:S.Tail, "--lead", 0) }
    if ($script:S.Clock)    { $a += "--clock" }
    if ($script:S.Region)   { $a += @("--region", $script:S.Region) }

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $script:Python
    $psi.Arguments = (($a | ForEach-Object {
        if ("$_" -match '\s') { '"' + $_ + '"' } else { "$_" } }) -join ' ')
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.CreateNoWindow = $true
    $proc = [System.Diagnostics.Process]::Start($psi)

    # Draw once immediately. The first progress line doesn't arrive until after
    # the Pro Tools pre-roll and the lead-in bar, and an empty screen for those
    # several seconds reads as a crash.
    $script:S.Message = "starting Pro Tools, then rolling..."
    Render

    while ($true) {
        if ($proc.HasExited -and $proc.StandardOutput.EndOfStream) { break }
        $ln = $proc.StandardOutput.ReadLine()
        if ($null -eq $ln) { if ($proc.HasExited) { break } else { continue } }

        # per-track mode:  " 1/20  Track 1   ch1   0:17.70  -> 01 Track 1"
        if ($ln -match '^\s+(\d+)/(\d+)\s+(.{1,24}?)\s+ch(\d+)\s+(\d+:\d+\.\d+)\s+->\s+(.+)$') {
            $script:S.CurPart  = [int]$Matches[1]
            $script:S.CurName  = $Matches[3].Trim()
            $script:S.CurCh    = [int]$Matches[4]
            $script:S.Elapsed  = $Matches[5]
            $script:S.CurTrack = $Matches[6].Trim()
            $script:S.Cues    += $Matches[5]
            $tot = [int]$Matches[2]
            if ($tot -gt 0) { $script:S.PassPct = [int](100.0 * $script:S.CurPart / $tot) }
            $script:S.Message = ""
            Render
            continue
        }
        if ($ln -match 'FAILED:') { $script:S.Message = $ln.Trim(); Render; continue }
        if ($ln -match 'Mode: one Pro Tools track') { $script:S.Message = "per-track mode"; Render; continue }

        if ($ln -match '\[(\d+:\d+\.\d+)\]\s+part (\d+)/(\d+)\s+(.{1,24}?)\s+ch(\d+)') {
            $script:S.Elapsed = $Matches[1]
            $script:S.CurPart = [int]$Matches[2]
            $script:S.CurName = $Matches[4].Trim()
            $script:S.CurCh   = [int]$Matches[5]
            $script:S.Cues   += $Matches[1]
            $tot = [int]$Matches[3]
            if ($tot -gt 0) { $script:S.PassPct = [int](100.0 * $script:S.CurPart / $tot) }
            Render
        }
        elseif ($ln -match 'Total pass length:\s+(\S+)') { $script:S.Total = $Matches[1]; Render }
        elseif ($ln -match 'Done\. (\d+) track\(s\) recorded') {
            $script:S.Markers = [int]$Matches[1]; $script:S.Message = "$($Matches[1]) track(s) recorded"
        }
        elseif ($ln -match 'Done\. (\d+) messages')      { $script:S.Sent = [int]$Matches[1] }
        elseif ($ln -match 'mean lateness ([\d.]+) ms, worst ([\d.]+)') {
            $script:S.MeanLate = [double]$Matches[1]; $script:S.WorstLate = [double]$Matches[2]
        }
        elseif ($ln -match '(\d+)/(\d+) marker') { $script:S.Markers = [int]$Matches[1] }

        if ([Console]::KeyAvailable) {
            $k = [Console]::ReadKey($true)
            if ($k.Key -eq "F8" -or $k.Key -eq "T") { Switch-Theme; Render }
            elseif ($k.Key -eq "Escape") {
                try { $proc.Kill() } catch { }
                $script:S.Message = "Aborted by user"
                break
            }
        }
    }
    try { $proc.WaitForExit() } catch { }
    $script:S.Phase = "done"; $script:S.PassPct = 100
    Clear-Screen
}

# =================================================================== demo ===

if ($Demo) {
    Enable-VirtualTerminal
    if (-not $script:S.SongFile) { $script:S.SongFile = "" }
    Update-Device; Update-ProTools; Update-Song
    $script:S.Total = "19:02.94"; $script:S.MeanLate = 0.018; $script:S.WorstLate = 0.811
    $script:S.Markers = 20; $script:S.Sent = 9679; $script:S.PassPct = 41
    foreach ($t in $THEMES) {
        $script:S.Theme = $t
        Line ""
        Line ((C 'dgray') + ("=" * $W))
        Line ((C 'yellow') + "  THEME: " + $t.ToUpper())
        Line ((C 'dgray') + ("=" * $W))
        switch ($t) {
            "turbo"    { Render-Turbo }
            "phosphor" { Render-Phosphor }
            "ansi"     { Render-Ansi }
        }
    }
    Line ""
    return
}

# =================================================================== main ===

Enable-VirtualTerminal
Hide-Cursor
Clear-Screen
try {
    Update-Device
    Update-ProTools
    if ($script:S.SongFile) { Update-Song }

    while ($true) {
        Render
        $k = [Console]::ReadKey($true)
        switch ($k.Key) {
            "F1" { $script:S.Message = "F2 load  F3 region  F5 test  F6 arm  F8 theme  F9 capture  L loops  C clock" }
            "F2" {
                Show-Cursor; Clear-Screen
                Write-Host ""
                Write-Host "  MIDI files in $($script:Root):"
                Write-Host ""
                Get-ChildItem $script:Root -Filter *.mid | ForEach-Object { Write-Host "    $($_.Name)" }
                Write-Host ""
                $f = Read-Host "  Filename (blank to cancel)"
                if ($f) { $script:S.SongFile = $f; $script:S.Message = ""; Update-Song }
                Hide-Cursor; Clear-Screen
            }
            "F3" {
                Show-Cursor; Clear-Screen
                $r = Read-Host "`n  Region, e.g. 9-16 (blank = whole song)"
                $script:S.Region = $r
                Hide-Cursor; Clear-Screen
            }
            "F5" { Update-Device; $script:S.Message = "Device: $($script:S.UsbId) $($script:S.Endpoint)" }
            "F6" {
                try {
                    & node $script:PTools record-arm --name $script:S.PtTrack | Out-Null
                    Update-ProTools
                    $script:S.Message = "Armed $($script:S.PtTrack)"
                } catch { $script:S.Message = "Arm failed - is Pro Tools running?" }
            }
            "F8"  { Switch-Theme }
            "T"   { Switch-Theme }
            "F9"  { Start-Capture }
            "F10" { return }
            "Q"   { return }
            "L"   { if ($script:S.Loops -ge 4) { $script:S.Loops = 1 } else { $script:S.Loops++ } }
            "C"   { $script:S.Clock = -not $script:S.Clock }
            "Escape" { return }
        }
    }
}
finally {
    Show-Cursor
    [Console]::Write($RESET)
    Clear-Screen
}

