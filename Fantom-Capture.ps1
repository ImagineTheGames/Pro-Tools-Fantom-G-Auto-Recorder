<#
.SYNOPSIS
    Console front end for the Fantom stem-capture rig.

.DESCRIPTION
    One screen over three moving parts: the Fantom's raw-USB MIDI link, the song
    being captured, and the Pro Tools session receiving the audio. Drives a full
    pass without touching a transport button.

    One screen, 16-colour BBS art, with colour-coded columns for the results
    table and a live input meter during a pass.

.EXAMPLE
    .\Fantom-Capture.ps1
    .\Fantom-Capture.ps1 -Song TOGEEWIZARD.mid
    .\Fantom-Capture.ps1 -Demo          # render once and exit
#>

[CmdletBinding()]
param(
    [string]$Song  = "",
    [switch]$Demo
)

$ErrorActionPreference = "Stop"

$script:Root   = Split-Path -Parent $MyInvocation.MyCommand.Path
$script:Python = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
$script:Stem   = Join-Path $script:Root "fantom_stem.py"
$script:PTools = "C:\Users\Rei\protools-mcp-server\ptools.js"

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
    SongFile = $Song; SongName = "(none loaded)"
    Bpm = 0.0; Bars = 0.0; TimeSig = "4/4"; Parts = @()
    Loops = 2; Gap = 2; Region = ""; Clock = $false
    PerTrack = $true; Tail = 4.0; CurTrack = ""
    Session = "C:\ProTools\2026\OGWizard"
    Scroll = 0; Cursor = 0; Follow = $true
    DeviceOk = $false; UsbId = "-"; Endpoint = "-"
    PtSession = "not connected"; PtRate = "-"; PtTrack = "Fantom Stems"
    PtArmed = $false; PtOk = $false
    Phase = "idle"; CurPart = 0; CurName = ""; CurCh = 0; PassPct = 0
    Elapsed = "0:00.00"; Total = "0:00.00"; Sent = 0
    MeanLate = 0.0; WorstLate = 0.0; Markers = 0
    Message = ""; Cues = @()
    Levels = @{}; Peak = -999.0; Rms = -999.0; T0 = $null; Rec = $false
}

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
    $script:S.Parts  = $parts
    $script:S.Cursor = 0
    $script:S.Scroll = 0
    $script:S.Follow = $true
}

# ================================================================== meter ===
#
# Where a level reading can come from, given what is actually available:
#
#   PTSL          exposes no metering command at all.
#   the interface is held exclusively by the ASIO driver, so it cannot be
#                 opened a second time to listen in.
#   the record file IS readable while Pro Tools writes it.
#
# So the meter reads the tail of the file being recorded. Two Windows details
# make this work: the directory entry does not update while a file is open --
# only a handle reports the true length -- and Pro Tools flushes about every
# two seconds, which is the meter's real update rate. It lags a beat behind
# the sound in the room. It is not lying about the level.

$script:Meter = @{ Path = ""; Data = -1; Bits = 24; Size = 0; Pos = 0 }

function Find-RecordFile { param([string]$Track)
    $dir = Join-Path $script:S.Session "Audio Files"
    if (-not $Track -or -not (Test-Path $dir)) { return $null }
    # Sort by the take number in the name: LastWriteTime is stale on the very
    # file we care about, because Pro Tools still has it open.
    $f = Get-ChildItem $dir -Filter *.L.wav -ErrorAction SilentlyContinue |
         Where-Object { $_.Name.StartsWith($Track + "_") } |
         Sort-Object { [int]([regex]::Match($_.Name, '_(\d+)\.L\.wav$').Groups[1].Value) } -Descending |
         Select-Object -First 1
    if ($f) { return $f.FullName }
    return $null
}

function Get-WavLayout { param([IO.FileStream]$Fs)
    # Pro Tools puts JUNK, bext, minf and elm1 chunks ahead of the audio, so
    # the data offset is around 16 KB, not the textbook 44 bytes.
    $h = [byte[]]::new(65536)
    $Fs.Position = 0
    $n = $Fs.Read($h, 0, $h.Length)
    $i = 12
    $bits = 24
    while ($i + 8 -le $n) {
        $id = [Text.Encoding]::ASCII.GetString($h, $i, 4)
        $sz = [int64][BitConverter]::ToUInt32($h, $i + 4)
        if ($id -eq 'fmt ' -and $i + 24 -le $n) { $bits = [BitConverter]::ToUInt16($h, $i + 22) }
        if ($id -eq 'data') { return @{ Offset = $i + 8; Bits = $bits; Size = $sz } }
        $i += 8 + $sz + ($sz % 2)
        if ($sz -le 0) { break }
    }
    return @{ Offset = -1; Bits = $bits; Size = 0 }
}

function Update-Meter {
    $track = $script:S.CurTrack
    if (-not $track) { return }
    $path = Find-RecordFile $track
    if (-not $path) { return }
    if ($path -ne $script:Meter.Path) {
        $script:Meter.Path = $path
        $script:Meter.Data = -1
        $script:Meter.Pos  = 0
    }
    try { $fs = [IO.File]::Open($path, 'Open', 'Read', 'ReadWrite') } catch { return }
    try {
        if ($script:Meter.Data -lt 0) {
            $lay = Get-WavLayout $fs
            if ($lay.Offset -lt 0) { return }
            $script:Meter.Data = $lay.Offset
            $script:Meter.Bits = $lay.Bits
            $script:Meter.Size = $lay.Size
            $script:Meter.Pos  = $lay.Offset
        }
        $w   = [int]($script:Meter.Bits / 8)
        $len = $fs.Length

        # A finished file carries regn/umid chunks after the audio -- tens of
        # kilobytes of metadata that read as full-scale noise if you treat the
        # end of the file as the end of the audio. Trust the data chunk size
        # when it is filled in; while recording it is still zero, and then the
        # end of the file genuinely is the end of the audio.
        $end = $len
        $sz  = [int64]$script:Meter.Size
        if ($sz -gt 0 -and $script:Meter.Data + $sz -le $len) { $end = $script:Meter.Data + $sz }
        if ($end -le $script:Meter.Pos + $w) { return }

        # Only the newest audio matters. If a flush arrived while we were busy,
        # skip forward rather than working through the backlog.
        $window = 48000 * $w
        $from = [int64][math]::Max([double]$script:Meter.Pos, [double]($end - $window))
        $from = $from - (($from - $script:Meter.Data) % $w)
        $count = [int][math]::Min([double]($end - $from), 400000.0)
        $buf = [byte[]]::new($count)
        $fs.Position = $from
        $got = $fs.Read($buf, 0, $count)
        $script:Meter.Pos = $from + $got

        $ns = [int]($got / $w)
        if ($ns -lt 1) { return }
        # Cap the work at a fixed number of samples so the cost per poll is flat
        # however much audio arrived. Twelve thousand keeps a loop this shape
        # under about 15 ms, and decimating that lightly costs peak accuracy
        # well under a decibel on musical material.
        $step = [math]::Max(1, [int]($ns / 12000))
        $full = 8388608.0
        if ($w -eq 2) { $full = 32768.0 }
        $peak = 0.0; $acc = 0.0; $k = 0
        for ($s = 0; $s -lt $ns; $s += $step) {
            $o = $s * $w
            if ($w -eq 3) {
                $hi = [int]$buf[$o + 2]
                if ($hi -gt 127) { $hi -= 256 }      # PowerShell will not cast byte->sbyte
                # [int] on the left is load-bearing: -bor takes the type of its
                # left operand, so a [byte] there truncates the result to eight
                # bits and every sample comes back as its own low byte.
                $v = [double](([int]$buf[$o]) -bor (([int]$buf[$o + 1]) -shl 8) -bor ($hi -shl 16))
            } else {
                $v = [double][BitConverter]::ToInt16($buf, $o)
            }
            $a = [math]::Abs($v)
            if ($a -gt $peak) { $peak = $a }
            $acc += $v * $v
            $k++
        }
        if ($k -lt 1) { return }
        $script:S.Peak = Db ($peak / $full)
        $script:S.Rms  = Db ([math]::Sqrt($acc / $k) / $full)
        if ($script:S.CurPart -gt 0) {
            $n = $script:S.CurPart
            $held = $script:S.Levels[$n]
            if ($null -eq $held -or $script:S.Peak -gt $held) { $script:S.Levels[$n] = $script:S.Peak }
        }
    } finally { $fs.Close() }
}

function Db { param([double]$Frac)
    if ($Frac -le 0.0000001) { return -999.0 }
    return 20.0 * [math]::Log10($Frac)
}

# -60 dBFS empty through 0 dBFS full, coloured by headroom rather than by
# position, so the colour means the same thing whatever the bar length.
function Level-Bar { param([double]$Db, [int]$Width = 10)
    if ($Db -le -900) { return (C 'dgray') + (Rep 0x2591 $Width) }
    $f = ($Db + 60.0) / 60.0
    if ($f -lt 0) { $f = 0.0 }
    if ($f -gt 1) { $f = 1.0 }
    $n = [int][math]::Round($Width * $f)
    if ($n -lt 1 -and $Db -gt -900) { $n = 1 }
    $col = C 'lgreen'
    if ($Db -gt -12) { $col = C 'yellow' }
    if ($Db -gt -3)  { $col = C 'lred' }
    return $col + (Rep 0x2588 $n) + (C 'dgray') + (Rep 0x2591 ($Width - $n))
}

function Db-Text { param([double]$Db)
    if ($Db -le -900) { return "  --  " }
    return ("{0,6:0.0}" -f $Db)
}

function Get-VisibleRows { return 12 }

# Keep the cursor on screen: scroll only when it would leave the window.
function Clamp-Scroll { param([int]$Rows)
    $n = $script:S.Parts.Count
    if ($n -eq 0) { $script:S.Cursor = 0; $script:S.Scroll = 0; return 0 }
    if ($script:S.Cursor -lt 0) { $script:S.Cursor = 0 }
    if ($script:S.Cursor -ge $n) { $script:S.Cursor = $n - 1 }
    if ($script:S.Cursor -lt $script:S.Scroll) { $script:S.Scroll = $script:S.Cursor }
    if ($script:S.Cursor -ge $script:S.Scroll + $Rows) {
        $script:S.Scroll = $script:S.Cursor - $Rows + 1
    }
    $max = [math]::Max(0, $n - $Rows)
    if ($script:S.Scroll -gt $max) { $script:S.Scroll = $max }
    if ($script:S.Scroll -lt 0) { $script:S.Scroll = 0 }
    return $script:S.Scroll
}

function Move-Cursor { param([int]$Delta)
    $n = $script:S.Parts.Count
    if ($n -eq 0) { return }
    $script:S.Cursor = [math]::Max(0, [math]::Min($n - 1, $script:S.Cursor + $Delta))
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

# ============================================================ THEME: ANSI ===

# A 3-row block font, 5 columns per glyph. Enough to spell the banner in the
# BBS-art idiom the ANSI theme is quoting, without eating half the screen.
$GLYPHS = @{
    'F' = @('█████','███  ','█    ')
    'A' = @('▄███▄','█████','█   █')
    'N' = @('█▄  █','█ ▀▄█','█   █')
    'T' = @('█████','  █  ','  █  ')
    'O' = @('▄███▄','█   █','▀███▀')
    'M' = @('█▄ ▄█','█ ▀ █','█   █')
    'S' = @('▄████','▀███▄','████▀')
    'E' = @('█████','███  ','█████')
    ' ' = @('     ','     ','     ')
}

function Banner { param([string]$Text)
    $rows = @('', '', '')
    foreach ($ch in $Text.ToCharArray()) {
        $g = $GLYPHS["$ch"]
        if (-not $g) { $g = $GLYPHS[' '] }
        for ($r = 0; $r -lt 3; $r++) { $rows[$r] += $g[$r] + ' ' }
    }
    return $rows
}

function Render-Ansi {
    Line ""
    # drop shadow: the same glyphs offset a row and dimmed, as ANSI art did
    $b = Banner 'FANTOM'
    $tint = @((C 'lmagenta'), (C 'magenta'), (C 'magenta'))
    for ($r = 0; $r -lt 3; $r++) {
        Line ("  " + $tint[$r] + $b[$r] + (C 'dgray') + "  " +
              $(if ($r -eq 1) { (C 'lcyan') + "S T E M   C A P T U R E" } else { "" }))
    }
    Line ((C 'dgray') + "  " + (Rep 0x2500 72))

    $dot = [char]0x00B7
    $state = (CB 'black' 'lgray') + " READY " + $RESET
    if ($script:S.Phase -eq "running") {
        # A blinking red dot is the one thing that reads as "running" at a
        # glance -- a static label looks the same as a hung process.
        $blink = (C 'dgray') + [char]0x25CF
        if ($script:S.Rec) { $blink = (C 'lred') + [char]0x25CF }
        $state = (CB 'black' 'red') + " REC " + $RESET + " " + $blink
    }
    if ($script:S.Phase -eq "done") { $state = (CB 'black' 'lcyan') + " PASS COMPLETE " + $RESET }

    $what = "$($script:S.Parts.Count) parts"
    if ($script:S.Phase -eq "running" -and $script:S.CurPart -gt 0) {
        $what = "part $($script:S.CurPart)/$($script:S.Parts.Count)  $($script:S.CurName)"
    }
    Line ("  " + $state + " " + (C 'dgray') + $dot + " " + (C 'white') + $script:S.SongName +
          " " + (C 'dgray') + $dot + " " + (C 'yellow') + $what +
          " " + (C 'dgray') + $dot + " " + (C 'yellow') + "$($script:S.Elapsed) / $($script:S.Total)" +
          " " + (C 'dgray') + $dot + " " + (C 'lgreen') + "$($script:S.Markers) markers")

    # Tempo belongs on screen: it is the one number you have to match by hand
    # in Pro Tools, because PTSL has no way to set a session's tempo.
    $bpm = "-- BPM"
    if ($script:S.Bpm -gt 0) { $bpm = "{0:0.##} BPM" -f $script:S.Bpm }
    $bars = "-"
    if ($script:S.Bars -gt 0) { $bars = "{0:0.##} bars" -f $script:S.Bars }
    $reg = "whole song"
    if ($script:S.Region) { $reg = "bars $($script:S.Region)" }
    Line ("  " + (C 'lmagenta') + $bpm + " " + (C 'dgray') + $dot + " " +
          (C 'white') + $script:S.TimeSig + " " + (C 'dgray') + $dot + " " +
          (C 'lgray') + $bars + " " + (C 'dgray') + $dot + " " +
          (C 'lgray') + "$($script:S.Loops) loops" + " " + (C 'dgray') + $dot + " " +
          (C 'lgray') + "tail $($script:S.Tail)s" + " " + (C 'dgray') + $dot + " " +
          (C 'lgray') + $reg)

    # The pass bar: without it there is no sense of how far in you are.
    $full = 60
    $done = [int][math]::Round($full * ($script:S.PassPct / 100.0))
    if ($done -lt 0) { $done = 0 }
    if ($done -gt $full) { $done = $full }
    Line ("  " + (C 'lcyan') + (Rep 0x2588 $done) + (C 'dgray') + (Rep 0x2591 ($full - $done)) +
          " " + (C 'white') + ("{0,3}%" -f $script:S.PassPct))
    Line ((C 'dgray') + "  " + (Rep 0x2500 72))
    Line ((CB 'black' 'lcyan') + (Plain "  #   PART                  CH   KEEP AT      dBFS  LEVEL        MARK" $W))

    $rows = 12
    if ($script:S.Parts.Count -eq 0) {
        Line ((C 'dgray') + "       no song loaded " + [char]0x2014 + " press F2")
    } else {
        $top = Clamp-Scroll $rows
        for ($i = 0; $i -lt $rows; $i++) {
            $idx = $top + $i
            if ($idx -ge $script:S.Parts.Count) { Line ""; continue }
            $p = $script:S.Parts[$idx]
            $keep = "-"
            $mark = (C 'dgray') + $dot
            if ($script:S.Cues.Count -gt $idx) {
                $keep = $script:S.Cues[$idx]
                $mark = (C 'lgreen') + [char]0x221A
            }
            # The level is measured, not inferred: live off the record file for
            # the part being captured, held at its peak for parts already done.
            $live = ($script:S.Phase -eq "running" -and $p.N -eq $script:S.CurPart)
            if ($live) {
                $db = $script:S.Peak
            } else {
                $db = $script:S.Levels[$p.N]
                if ($null -eq $db) { $db = -999.0 }
            }
            $meter = Level-Bar $db 10
            if ($live) {
                $mark = (C 'lred') + [char]0x25CF
                if (-not $script:S.Rec) { $mark = (C 'red') + [char]0x25CF }
            }
            $cur = "  "
            if ($idx -eq $script:S.Cursor) { $cur = (C 'lcyan') + [char]0x25BA + " " }
            Line ($cur + (C 'dgray') + ("{0:00}" -f $p.N) + "  " + (C 'white') + (Plain $p.Name 20) +
                  " " + (C 'lblue') + ("{0,2}" -f $p.Ch) + "   " + (C 'yellow') + (Plain $keep 10) +
                  " " + (C 'dgray') + (Db-Text $db) + "  " + $meter + "   " + $mark)
        }
        $shown = [math]::Min($rows, $script:S.Parts.Count - $top)
        Line ((C 'dgray') + ("  {0}-{1} of {2}   " -f ($top + 1), ($top + $shown),
              $script:S.Parts.Count) + [char]0x2191 + [char]0x2193 + " scroll")
    }

    $clkTxt = "off"
    if ($script:S.Clock) { $clkTxt = "24 PPQN locked" }
    Line ((C 'dgray') + "  " + (Rep 0x2500 72))
    Line ((C 'lcyan') + "  TIMING  " + (C 'lgray') +
          ("mean {0:0.000} ms " -f $script:S.MeanLate) + $dot +
          (" worst {0:0.000} ms" -f $script:S.WorstLate) + "      " +
          (C 'lcyan') + "CLOCK  " + (C 'lgray') + $clkTxt)
    $src = $script:S.PtTrack
    if ($script:S.CurTrack) { $src = $script:S.CurTrack }
    Line ((C 'lcyan') + "  AUDIO   " + (C 'lgray') + "$($script:S.PtRate) " + $dot +
          " $src " + $dot + " $($script:S.PtSession)")
    if ($script:S.Phase -eq "running") {
        Line ((C 'lcyan') + "  INPUT   " + (Level-Bar $script:S.Peak 24) + " " +
              (C 'white') + (Db-Text $script:S.Peak) + (C 'dgray') + " peak   " +
              (C 'lgray') + (Db-Text $script:S.Rms) + (C 'dgray') + " rms" +
              "   (from the record file, ~2 s behind)")
    }
    if ($script:S.Message) { Line ((C 'brown') + "  NOTE    $($script:S.Message)") }
    Line ((C 'dgray') + "  " + (Rep 0x2500 72))
    Line ("  " + (C 'lgreen') + "[F2]" + (C 'lgray') + " song  " +
          (C 'lgreen') + "[F3]" + (C 'lgray') + " region  " +
          (C 'lgreen') + "[F6]" + (C 'lgray') + " arm  " +
          (C 'lgreen') + "[F7]" + (C 'lgray') + " verify  " +
          (C 'lgreen') + "[A]" + (C 'lgray') + " trim  " +
          (C 'lgreen') + "[L]" + (C 'lgray') + " loops  " +
          (C 'lgreen') + "[F9]" + (C 'lgray') + " capture  " +
          (C 'lred')   + "[S]"  + (C 'lgray') + " stop  " +
          (C 'lgreen') + "[F10]" + (C 'lgray') + " quit")
}

# ================================================================= render ===

function Render {
    Home
    Render-Ansi
    [Console]::Write("$RESET$E[J")
}

# ================================================================ capture ===

function Start-Capture {
    if (-not $script:S.SongFile) { $script:S.Message = "Load a song first (F2)"; return }
    $script:S.Phase = "running"; $script:S.PassPct = 0; $script:S.Cues = @(); $script:S.Message = ""
    $script:S.Follow = $true
    Clear-Screen

    # -u matters: with stdout on a pipe Python block-buffers 8 KB, so progress
    # lines sat in the buffer instead of reaching the screen.
    $a = @("-u", $script:Stem, "run", (Join-Path $script:Root $script:S.SongFile),
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

    # Read the child's output through an event into a queue rather than calling
    # ReadLine. In per-track mode a part only prints its line once it has
    # finished, so a blocking read froze the whole interface -- no clock, no
    # meter, no keys -- for the forty seconds each part takes.
    $q = [System.Collections.Queue]::Synchronized((New-Object System.Collections.Queue))
    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    $proc.EnableRaisingEvents = $true
    $sub = Register-ObjectEvent -InputObject $proc -EventName OutputDataReceived `
        -MessageData $q -Action { if ($null -ne $EventArgs.Data) { $Event.MessageData.Enqueue($EventArgs.Data) } }
    [void]$proc.Start()
    $proc.BeginOutputReadLine()

    $script:S.T0 = Get-Date
    $script:S.Levels = @{}
    $script:S.Peak = -999.0
    $script:S.Message = "starting Pro Tools, then rolling..."
    Render

    # Keep every line the child printed. The head trim reports at the very end
    # of the pass, and this screen used to be cleared the moment the process
    # exited -- so its output existed for a few milliseconds and was gone.
    $log = New-Object System.Collections.ArrayList

    $tick = 0
    while ($true) {
        if ($proc.HasExited -and $q.Count -eq 0) { break }

        while ($q.Count -gt 0) {
            $ln = [string]$q.Dequeue()
            [void]$log.Add($ln)
            Read-CaptureLine $ln
        }

        # Everything that must keep moving while the child is silent.
        $tick++
        $script:S.Rec = (($tick % 4) -lt 2)
        if ($script:S.T0) {
            $el = (Get-Date) - $script:S.T0
            $script:S.Elapsed = "{0}:{1:00}.{2:00}" -f `
                [int]$el.TotalMinutes, $el.Seconds, [int]($el.Milliseconds / 10)
        }
        Update-Meter
        Render

        if ([Console]::KeyAvailable) {
            $k = [Console]::ReadKey($true)
            if ($k.Key -eq "UpArrow")   { $script:S.Follow = $false; Move-Cursor -1 }
            elseif ($k.Key -eq "DownArrow") { $script:S.Follow = $false; Move-Cursor  1 }
            elseif ($k.Key -eq "PageUp")    { $script:S.Follow = $false; Move-Cursor (-1 * (Get-VisibleRows)) }
            elseif ($k.Key -eq "PageDown")  { $script:S.Follow = $false; Move-Cursor (Get-VisibleRows) }
            elseif ($k.Key -eq "Home")      { $script:S.Follow = $false; $script:S.Cursor = 0 }
            elseif ($k.Key -eq "End")       { $script:S.Follow = $false
                                              $script:S.Cursor = [math]::Max(0, $script:S.Parts.Count - 1) }
            elseif ($k.Key -eq "F")         { $script:S.Follow = $true }
            elseif ($k.Key -eq "Escape") {
                try { $proc.Kill() } catch { }
                $script:S.Message = "Aborted by user"
                break
            }
        }

        # The list follows the part being recorded until you scroll yourself;
        # after that it stays where you put it.
        if ($script:S.Follow -and $script:S.CurPart -gt 0) {
            $script:S.Cursor = $script:S.CurPart - 1
        }

        Start-Sleep -Milliseconds 120
    }

    if ($sub) { Unregister-Event -SubscriptionId $sub.Id -ErrorAction SilentlyContinue }
    try { $proc.WaitForExit(2000) } catch { }
    $script:S.Phase = "done"; $script:S.PassPct = 100; $script:S.Rec = $false

    Show-CaptureReport $log
    Clear-Screen
}

function Show-CaptureReport($log) {
    <#
        Show what the pass actually did, and wait.

        The trim runs after the last take, so its report is the last thing the
        child prints -- exactly the part that used to be wiped. Everything from
        the trim banner onward is shown; failing that, the tail of the run.
    #>
    if (-not $log -or $log.Count -eq 0) { return }

    $start = -1
    for ($i = 0; $i -lt $log.Count; $i++) {
        if ($log[$i] -match 'Trimming heads|HEAD TRIM') { $start = $i; break }
    }
    if ($start -lt 0) { $start = [Math]::Max(0, $log.Count - 12) }

    Show-Cursor
    Clear-Screen
    Write-Host ""
    foreach ($line in $log[$start..($log.Count - 1)]) { Write-Host $line }
    Write-Host ""
    Write-Host "  press a key..." -NoNewline
    [void][Console]::ReadKey($true)
    Hide-Cursor
}

# One line of the recorder's output, folded into the display state. Kept apart
# from the loop so the loop is only about keeping the screen alive.
function Read-CaptureLine { param([string]$ln)
    # ">>  3/20  Track 3   ch6  recording -> 03 Track 3" -- printed as the part
    # STARTS. This is what tells the meter which file to watch.
    if ($ln -match '^\s*>>\s*(\d+)/(\d+)\s+(.{1,24}?)\s+ch(\d+)\s+recording\s+->\s+(.+)$') {
        $n = [int]$Matches[1]
        if ($n -lt $script:S.CurPart) { return }
        $script:S.CurPart  = $n
        $script:S.CurName  = $Matches[3].Trim()
        $script:S.CurCh    = [int]$Matches[4]
        $script:S.CurTrack = $Matches[5].Trim()
        $tot = [int]$Matches[2]
        if ($tot -gt 0) { $script:S.PassPct = [int](100.0 * ($n - 1) / $tot) }
        $script:S.Message = ""
        $script:Meter.Path = ""      # new take, new file to follow
        $script:S.Peak = -999.0
        $script:S.Rms  = -999.0
        return
    }
    # " 1/20  Track 1   ch1   0:17.70  -> 01 Track 1" -- printed when it ends.
    # A part's closing line and the next part's opening line are printed back to
    # back, and the two can reach us in either order. Never let a lower part
    # number take over: doing so would aim the meter at a file Pro Tools has
    # already closed, and the level would sit dead for the whole next take.
    if ($ln -match '^\s+(\d+)/(\d+)\s+(.{1,24}?)\s+ch(\d+)\s+(\d+:\d+\.\d+)\s+->\s+(.+)$') {
        $n = [int]$Matches[1]
        $script:S.Cues += $Matches[5]
        $tot = [int]$Matches[2]
        if ($n -lt $script:S.CurPart) { return }
        $script:S.CurPart  = $n
        $script:S.CurName  = $Matches[3].Trim()
        $script:S.CurCh    = [int]$Matches[4]
        $script:S.CurTrack = $Matches[6].Trim()
        if ($tot -gt 0) { $script:S.PassPct = [int](100.0 * $n / $tot) }
        $script:S.Message = ""
        return
    }
    if ($ln -match 'FAILED:') { $script:S.Message = $ln.Trim(); return }
    if ($ln -match 'Mode: one Pro Tools track') { $script:S.Message = "per-track mode"; return }

    if ($ln -match '\[(\d+:\d+\.\d+)\]\s+part (\d+)/(\d+)\s+(.{1,24}?)\s+ch(\d+)') {
        $script:S.CurPart = [int]$Matches[2]
        $script:S.CurName = $Matches[4].Trim()
        $script:S.CurCh   = [int]$Matches[5]
        $script:S.Cues   += $Matches[1]
        $tot = [int]$Matches[3]
        if ($tot -gt 0) { $script:S.PassPct = [int](100.0 * $script:S.CurPart / $tot) }
    }
    elseif ($ln -match 'Total pass length:\s+(\S+)') { $script:S.Total = $Matches[1] }
    elseif ($ln -match 'Done\. (\d+) track\(s\) recorded') {
        $script:S.Markers = [int]$Matches[1]; $script:S.Message = "$($Matches[1]) track(s) recorded"
    }
    elseif ($ln -match 'Done\. (\d+) messages') { $script:S.Sent = [int]$Matches[1] }
    elseif ($ln -match 'mean lateness ([\d.]+) ms, worst ([\d.]+)') {
        $script:S.MeanLate = [double]$Matches[1]; $script:S.WorstLate = [double]$Matches[2]
    }
    elseif ($ln -match '(\d+)/(\d+) marker') { $script:S.Markers = [int]$Matches[1] }
}

# ================================================================= picker ===
#
# Choosing a song by typing its filename meant knowing the filename. This is a
# list you drive with the arrow keys; typing narrows it rather than naming it,
# so a few letters anywhere in the name is enough.

function Picker-Accent { return @((C 'lcyan'), (C 'white'), (C 'dgray')) }

# $Keys feeds keystrokes in place of the keyboard, so the picker can be driven
# and checked without a console attached.
function Show-SongPicker { param([object[]]$Keys)
    $ki = 0
    $all = @(Get-ChildItem $script:Root -Filter *.mid -ErrorAction SilentlyContinue |
             Sort-Object Name)
    if ($all.Count -eq 0) {
        $script:S.Message = "no .mid files in $($script:Root)"
        return $null
    }

    # NOT $pTxt: variable names are case-insensitive here, so a local $pTxt would be
    # the same variable as the global $pTxt palette table and would replace it with
    # a colour string -- silently breaking every colour drawn afterwards.
    $pAcc, $pTxt, $pDim = Picker-Accent
    $rows = 14
    $filter = ""
    $cur = 0
    # Open on the song already loaded, so re-picking is one keystroke away.
    if ($script:S.SongFile) {
        $i = [array]::IndexOf(($all | ForEach-Object { $_.Name }), $script:S.SongFile)
        if ($i -ge 0) { $cur = $i }
    }
    $top = 0
    # Scripted runs draw inline instead of taking over the screen, so the
    # picker can be rendered and checked alongside everything else.
    $scripted = ($null -ne $Keys)
    if (-not $scripted) { Clear-Screen }

    while ($true) {
        # Match on plain text, not -like: a bracket in a filename is a wildcard
        # to -like and would quietly match nothing.
        $needle = $filter.ToLower()
        $list = @($all | Where-Object { $_.Name.ToLower().Contains($needle) })

        if ($list.Count -eq 0) { $cur = 0; $top = 0 }
        else {
            if ($cur -ge $list.Count) { $cur = $list.Count - 1 }
            if ($cur -lt 0) { $cur = 0 }
            if ($cur -lt $top) { $top = $cur }
            if ($cur -ge $top + $rows) { $top = $cur - $rows + 1 }
            $lim = [math]::Max(0, $list.Count - $rows)
            if ($top -gt $lim) { $top = $lim }
            if ($top -lt 0) { $top = 0 }
        }

        if (-not $scripted) { Home }
        Line ""
        Line ($pAcc + "  SELECT SONG" + $pDim + "   $($script:Root)")
        Line ($pDim + "  " + (Rep 0x2500 72))

        $shownFilter = "(all)"
        if ($filter) { $shownFilter = $filter }
        Line ($pDim + "  find  " + $pAcc + (Plain $shownFilter 30) + $pDim +
              "$($list.Count) of $($all.Count)")
        Line ""

        if ($list.Count -eq 0) {
            Line ($pDim + "    nothing matches " + [char]0x2014 + " Backspace to widen")
            for ($i = 1; $i -lt $rows; $i++) { Line "" }
        } else {
            for ($i = 0; $i -lt $rows; $i++) {
                $idx = $top + $i
                if ($idx -ge $list.Count) { Line ""; continue }
                $f = $list[$idx]
                $kb = "{0,6:0} KB" -f ($f.Length / 1KB)
                $when = $f.LastWriteTime.ToString("yyyy-MM-dd HH:mm")
                if ($idx -eq $cur) {
                    Line ((CB 'black' 'cyan') + (Plain ("  > " + $f.Name) 46) +
                          (Plain "$kb   $when" 30) + $RESET)
                } else {
                    Line ($pDim + "    " + $pTxt + (Plain $f.Name 42) + $pDim + "$kb   $when")
                }
            }
            $shown = [math]::Min($rows, $list.Count - $top)
            Line ($pDim + ("    {0}-{1} of {2}" -f ($top + 1), ($top + $shown), $list.Count))
        }

        Line ""
        Line ($pDim + "  " + (Rep 0x2500 72))
        Line ($pDim + "  " + [char]0x2191 + [char]0x2193 + " move   PgUp/PgDn   Home/End   " +
              "type to filter   Backspace   Enter select   Esc cancel")
        [Console]::Write("$RESET$E[J")

        # if/elseif rather than switch: inside a switch, 'continue' belongs to
        # the switch, not to this loop, and the difference is easy to miss.
        if ($null -ne $Keys) {
            if ($ki -ge $Keys.Count) { if (-not $scripted) { Clear-Screen }; return $null }
            $k = $Keys[$ki]; $ki++
        } else {
            $k = [Console]::ReadKey($true)
        }
        if     ($k.Key -eq "UpArrow")   { $cur-- }
        elseif ($k.Key -eq "DownArrow") { $cur++ }
        elseif ($k.Key -eq "PageUp")    { $cur -= $rows }
        elseif ($k.Key -eq "PageDown")  { $cur += $rows }
        elseif ($k.Key -eq "Home")      { $cur = 0 }
        elseif ($k.Key -eq "End")       { $cur = $list.Count - 1 }
        elseif ($k.Key -eq "Enter") {
            if ($list.Count -gt 0) { if (-not $scripted) { Clear-Screen }; return $list[$cur].Name }
        }
        elseif ($k.Key -eq "Backspace") {
            if ($filter.Length -gt 0) { $filter = $filter.Substring(0, $filter.Length - 1) }
        }
        elseif ($k.Key -eq "Escape") {
            # Esc clears a filter first, so a mistyped search doesn't throw you
            # out of the picker entirely.
            if ($filter) { $filter = "" } else { if (-not $scripted) { Clear-Screen }; return $null }
        }
        elseif ("$($k.KeyChar)" -match '^[A-Za-z0-9 ._\-]$') { $filter += $k.KeyChar }
    }
}

# =================================================================== demo ===

if ($Demo) {
    Enable-VirtualTerminal
    if (-not $script:S.SongFile) { $script:S.SongFile = "TOGEEWIZARD.mid" }
    Update-Device; Update-ProTools; Update-Song
    $script:S.Total = "19:02.94"; $script:S.MeanLate = 0.018; $script:S.WorstLate = 0.811
    $script:S.Markers = 20; $script:S.Sent = 9679; $script:S.PassPct = 41
    # Mid-pass, so the demo shows what a capture actually looks like.
    $script:S.Phase = "running"; $script:S.CurPart = 6; $script:S.CurName = "Track 6"
    $script:S.CurTrack = "06 Track 6"; $script:S.Cursor = 5; $script:S.Rec = $true
    $script:S.Elapsed = "7:48.10"; $script:S.Peak = -9.4; $script:S.Rms = -21.7
    $script:S.Levels = @{ 1 = -12.3; 2 = -6.1; 3 = -24.8; 4 = -2.2; 5 = -41.0 }
    $script:S.Cues = @("0:17.70","1:35.40","2:53.10","4:10.80","5:28.50")
    Render-Ansi
    Line ""
    Line ((C 'dgray') + ("=" * $W))
    Line ((C 'yellow') + "  SONG PICKER  (F2)")
    Line ((C 'dgray') + ("=" * $W))
    $esc = New-Object System.ConsoleKeyInfo(
        [char]27, [System.ConsoleKey]::Escape, $false, $false, $false)
    $null = Show-SongPicker -Keys @($esc)
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
            "F1" { $script:S.Message = "F2 song  F3 region  F5 test  F6 arm  F7 verify  A trim  F9 capture  S stop  L loops  C clock  " +
                                       [char]0x2191 + [char]0x2193 + "/PgUp/PgDn/Home/End scroll parts  F follow" }
            "F2" {
                $f = Show-SongPicker
                if ($f) { $script:S.SongFile = $f; $script:S.Message = ""; Update-Song }
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
            "F7"  {
                # measure the takes -- you can't listen to an unattended pass
                Show-Cursor; Clear-Screen
                & $script:Python $script:Stem verify $script:S.Session
                Write-Host ""
                Write-Host "  press a key..." -NoNewline
                [void][Console]::ReadKey($true)
                Hide-Cursor; Clear-Screen
            }
            "A"   {
                Show-Cursor; Clear-Screen
                if (-not $script:S.SongFile) {
                    Write-Host "`n  Load a song first (F2)."
                } else {
                    # Tab to transient, split, delete left, shift left -- with
                    # --grid so parts that do not begin on beat 1 keep their place.
                    $mid = Join-Path $script:Root $script:S.SongFile
                    & $script:Python $script:Stem tab $script:S.Session `
                        --grid $mid --fill --dry-run
                    Write-Host ""
                    $go = Read-Host "  Apply this trim? (y/N)"
                    if ($go -eq 'y') {
                        & $script:Python $script:Stem tab $script:S.Session `
                            --grid $mid --fill --yes
                    }
                }
                Write-Host ""
                Write-Host "  press a key..." -NoNewline
                [void][Console]::ReadKey($true)
                Hide-Cursor; Clear-Screen
            }
            "F9"  { Start-Capture }
            "S"   {
                # Panic button. A crashed or abandoned capture leaves Pro Tools
                # rolling and armed, the Fantom sounding, and orphaned
                # processes. Quitting this console (F10/Q/Esc) does none of
                # that, which is why this key exists separately.
                Show-Cursor; Clear-Screen
                $stop = Join-Path $script:Root "Stop-Capture.ps1"
                if (Test-Path $stop) {
                    & powershell -NoProfile -ExecutionPolicy Bypass -File $stop
                } else {
                    Write-Host "`n  Stop-Capture.ps1 not found beside this script."
                }
                Write-Host ""
                Write-Host "  press a key..." -NoNewline
                [void][Console]::ReadKey($true)
                Hide-Cursor; Clear-Screen
                Update-ProTools
            }
            "F10" { return }
            "Q"   { return }
            "L"   { if ($script:S.Loops -ge 4) { $script:S.Loops = 1 } else { $script:S.Loops++ } }
            "C"   { $script:S.Clock = -not $script:S.Clock }

            # Track list navigation. Each theme shows a different number of
            # rows, so a page is whatever the current theme can display.
            "UpArrow"    { Move-Cursor -1 }
            "DownArrow"  { Move-Cursor  1 }
            "PageUp"     { Move-Cursor (-1 * (Get-VisibleRows)) }
            "PageDown"   { Move-Cursor (Get-VisibleRows) }
            "Home"       { $script:S.Cursor = 0 }
            "End"        { $script:S.Cursor = [math]::Max(0, $script:S.Parts.Count - 1) }

            "Escape" { return }
        }
    }
}
finally {
    Show-Cursor
    [Console]::Write($RESET)
    Clear-Screen
}
