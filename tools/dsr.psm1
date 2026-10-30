# Driver for launching and steering Dark Souls Remastered during automated tests.
# Import-Module .\tools\dsr.psm1 ; all functions are offline-test helpers, never used for online play.

$script:Root = Split-Path -Parent $PSScriptRoot
$script:GameDir = "C:\Program Files (x86)\Steam\steamapps\common\DARK SOULS REMASTERED"
$script:SaveDir = Join-Path $env:USERPROFILE "Documents\NBGI\DARK SOULS REMASTERED\<Steam account folder>"
$script:LiveSave = Join-Path $script:SaveDir "DRAKS0005.sl2"
$script:KeysIni = Join-Path $env:LOCALAPPDATA "FromSoftware\NBGI\DarkSouls\DarkSoulsKeys.ini"
$script:Ledger = Join-Path $script:Root "saves\ledger.json"

if (-not ("DsrInput" -as [type])) {
    Add-Type -TypeDefinition (Get-Content -Raw (Join-Path $PSScriptRoot "input\DsrInput.cs")) -ReferencedAssemblies System.Drawing
}
[DsrInput]::EnsureDpiAware()

# Keyboard bindings: first [KEYBOARD_AND_MOUSE] value per action (later sections are modifiers/pad).
function Get-DsrKeyMap {
    $map = @{}
    $section = ""
    foreach ($line in Get-Content $script:KeysIni) {
        if ($line -match '^\[(.+)\]') { $section = $Matches[1]; continue }
        if ($section -ne "KEYBOARD_AND_MOUSE") { continue }
        if ($line -match '^ACTION_(\w+?) = (\d+)$' -and $line -notmatch '_mod =') {
            if (-not $map.ContainsKey($Matches[1])) { $map[$Matches[1]] = [int]$Matches[2] }
        }
    }
    return $map
}
$script:Keys = Get-DsrKeyMap
$script:Aliases = @{
    Up = "MenuCtrl_Up"; Down = "MenuCtrl_Down"; Left = "MenuCtrl_Left"; Right = "MenuCtrl_Right"
    Confirm = "MenuCtrl_Enterv"; Cancel = "MenuCtrl_Cancel"; Menu = "MenuDisp_MenuDisp"
}

function Test-DsrRunning { [bool](Get-Process -Name DarkSoulsRemastered -ErrorAction SilentlyContinue) }

function Get-FileSha([string]$Path) { (Get-FileHash $Path -Algorithm SHA256).Hash.ToLower() }

# Refuse to run unless the live save is the recorded original or a recorded snapshot.
function Assert-DsrSaveKnown {
    $ledger = Get-Content -Raw $script:Ledger | ConvertFrom-Json
    $live = Get-FileSha $script:LiveSave
    $known = @($ledger.original_save.sha256) + @($ledger.snapshots.PSObject.Properties | ForEach-Object { $_.Value.sha256 })
    if ($known -notcontains $live) {
        Write-Warning "Live save hash $live is not in the ledger (game played since last backup?)."
        return $false
    }
    return $true
}

function Start-Dsr([int]$TimeoutSec = 120) {
    if (Test-DsrRunning) { throw "DSR is already running." }
    if (-not (Get-Process -Name steam -ErrorAction SilentlyContinue)) { throw "Steam is not running." }
    Start-Process "steam://rungameid/570940"
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $h = [DsrInput]::FindGameWindow()
        if ($h -ne [IntPtr]::Zero) { return $h }
        Start-Sleep -Milliseconds 500
    }
    throw "DSR window did not appear within $TimeoutSec s."
}

function Get-DsrWindow { [DsrInput]::FindGameWindow() }

function Send-DsrKey([string]$Action, [int]$Times = 1, [int]$HoldMs = 80, [int]$GapMs = 250) {
    $name = if ($script:Aliases.ContainsKey($Action)) { $script:Aliases[$Action] } else { $Action }
    if (-not $script:Keys.ContainsKey($name)) { throw "Unknown key action '$Action'." }
    $h = Get-DsrWindow
    if (-not [DsrInput]::Focus($h)) { throw "Could not focus the DSR window." }
    for ($i = 0; $i -lt $Times; $i++) {
        [DsrInput]::Tap($script:Keys[$name], $HoldMs)
        Start-Sleep -Milliseconds $GapMs
    }
}

# Capture the game window. Writes full PNG if -Path given; always returns mean luma and a 960px thumb path.
function Get-DsrShot([string]$Path, [string]$Mode = "screen", [string]$ThumbPath) {
    $h = Get-DsrWindow
    [void][DsrInput]::Focus($h)
    $bmp = [DsrInput]::Grab($h, $Mode)
    try {
        $luma = [DsrInput]::MeanLuma($bmp)
        if ($Path) { [DsrInput]::Save($bmp, $Path) }
        if ($ThumbPath) { $t = [DsrInput]::Thumb($bmp, 960); [DsrInput]::Save($t, $ThumbPath); $t.Dispose() }
        [pscustomobject]@{ Luma = [math]::Round($luma, 2); Width = $bmp.Width; Height = $bmp.Height; Path = $Path; Thumb = $ThumbPath }
    } finally { $bmp.Dispose() }
}

# Poll until mean luma stays within $Tolerance for $StableSec (exposure settled / no fades).
function Wait-DsrStable([double]$StableSec = 6, [double]$Tolerance = 0.6, [int]$TimeoutSec = 90, [double]$MinLuma = 2) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $since = $null; $last = -100
    while ((Get-Date) -lt $deadline) {
        $l = (Get-DsrShot).Luma
        if ($l -ge $MinLuma -and [math]::Abs($l - $last) -le $Tolerance) {
            if (-not $since) { $since = Get-Date }
            if (((Get-Date) - $since).TotalSeconds -ge $StableSec) { return $l }
        } else { $since = $null }
        $last = $l
        Start-Sleep -Milliseconds 700
    }
    return $null
}

function Stop-DsrHard {
    Get-Process -Name DarkSoulsRemastered -ErrorAction SilentlyContinue | Stop-Process -Force
}

# HUD/menu regions masked out of image stats (4K pixel coords): HP/stamina + FPS overlay, item slots, souls.
$script:HudMasks = [int[]]@(0, 0, 1700, 460, 240, 1120, 1160, 2010, 3080, 1880, 3560, 2030)

function Test-Rgb($c, $r, $g, $b, [int]$tol = 25) {
    ([math]::Abs($c.R - $r) -le $tol) -and ([math]::Abs($c.G - $g) -le $tol) -and ([math]::Abs($c.B - $b) -le $tol)
}

# Probe pixels calibrated on 960x540 thumbnails (x4 at 4K). States: InGame, OfflineDialog, MainMenu, Dark, Other.
function Get-DsrState {
    $h = Get-DsrWindow
    if ($h -eq [IntPtr]::Zero) { return "NoWindow" }
    $bmp = [DsrInput]::Grab($h, "screen")
    try {
        $sx = $bmp.Width / 960.0; $sy = $bmp.Height / 540.0
        $px = { param($x, $y) [DsrInput]::PixelAt($bmp, [int]($x * $sx), [int]($y * $sy)) }
        $hp = & $px 250 61; $st = & $px 230 76
        if ((Test-Rgb $hp 189 90 86) -and (Test-Rgb $st 149 209 149)) { return "InGame" }
        $dlg = & $px 480 240
        if ($dlg.R -ge 80 -and $dlg.R -le 120 -and [math]::Abs($dlg.R - $dlg.B) -le 8) { return "OfflineDialog" }
        $row0 = & $px 480 395
        if ($row0.R -ge 170 -and $row0.G -ge 115 -and $row0.G -le 175 -and $row0.B -le 140) { return "MainMenu" }
        if ([DsrInput]::MeanLuma($bmp) -lt 30) { return "Dark" }
        return "Other"
    } finally { $bmp.Dispose() }
}

function Wait-DsrState([string[]]$States, [int]$TimeoutSec = 90) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $s = Get-DsrState
        if ($States -contains $s) { return $s }
        Start-Sleep -Milliseconds 600
    }
    return $null
}

# From boot/title/offline notice/main menu, press through to gameplay via "Continue". Each press is gated on a
# detected state, so no stray confirm reaches gameplay (where E uses the equipped item).
function Enter-DsrGame([int]$TimeoutSec = 180, [int]$SettleSec = 8) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $darkSince = $null
    while ((Get-Date) -lt $deadline) {
        switch (Get-DsrState) {
            "InGame" {
                [void](Wait-DsrStable -StableSec 4 -Tolerance 2 -TimeoutSec 60)
                Start-Sleep -Seconds $SettleSec   # let auto-exposure finish adapting
                return $true
            }
            "OfflineDialog" { Send-DsrKey Confirm; $darkSince = $null; Start-Sleep -Seconds 2 }
            "MainMenu" { Send-DsrKey Confirm; $darkSince = $null; Start-Sleep -Seconds 5 }
            "Dark" {
                # Title "press any button" or a loading screen: only press after it has been static a while.
                if (-not $darkSince) { $darkSince = Get-Date }
                elseif (((Get-Date) - $darkSince).TotalSeconds -ge 6) { Send-DsrKey Confirm; $darkSince = $null; Start-Sleep -Seconds 2 }
                else { Start-Sleep -Milliseconds 700 }
            }
            default { $darkSince = $null; Start-Sleep -Milliseconds 700 }
        }
    }
    throw "Did not reach gameplay within $TimeoutSec s."
}

# In-game: System -> Quit Game -> OK (dialog defaults to CANCEL). Saves and returns to "press any button".
function Exit-DsrToTitle {
    if ((Get-DsrState) -ne "InGame") { throw "Exit-DsrToTitle: not in game." }
    Send-DsrKey Menu; Start-Sleep -Seconds 2
    Send-DsrKey Right -Times 3 -GapMs 350; Start-Sleep -Milliseconds 500
    Send-DsrKey Confirm; Start-Sleep -Seconds 2
    Send-DsrKey Down -Times 5 -GapMs 300; Start-Sleep -Milliseconds 400
    Send-DsrKey Confirm; Start-Sleep -Seconds 2
    Send-DsrKey Left; Start-Sleep -Milliseconds 400
    Send-DsrKey Confirm
    if (-not (Wait-DsrState -States "Dark" -TimeoutSec 45)) { throw "Did not return to title." }
    Start-Sleep -Seconds 4
}

# Close the game from the title screen (after Exit-DsrToTitle saved). Avoids the title menu, whose
# "Log In" entry sits next to "Quit Game".
function Close-DsrAtTitle {
    $s = Get-DsrState
    if ($s -eq "InGame") { throw "Close-DsrAtTitle: still in game; call Exit-DsrToTitle first." }
    Stop-DsrHard
    Start-Sleep -Seconds 2
}

# Median of 3 frames -> full PNG, 960px thumb, and stats JSON (HUD masked). Returns the stats object.
function Invoke-DsrCapture([string]$OutDir, [string]$Name, [int]$GapMs = 900) {
    New-Item -ItemType Directory -Force $OutDir | Out-Null
    $h = Get-DsrWindow
    [void][DsrInput]::Focus($h)
    $frames = @()
    try {
        for ($i = 0; $i -lt 3; $i++) { $frames += [DsrInput]::Grab($h, "screen"); Start-Sleep -Milliseconds $GapMs }
        $med = [DsrInput]::Median3($frames[0], $frames[1], $frames[2])
        try {
            [DsrInput]::Save($med, (Join-Path $OutDir "$Name.png"))
            $t = [DsrInput]::Thumb($med, 960); [DsrInput]::Save($t, (Join-Path $OutDir "$Name.thumb.png")); $t.Dispose()
            $json = [DsrInput]::StatsJson($med, $script:HudMasks)
            Set-Content -Encoding ascii (Join-Path $OutDir "$Name.stats.json") $json
            return ($json | ConvertFrom-Json)
        } finally { $med.Dispose() }
    } finally { foreach ($f in $frames) { $f.Dispose() } }
}

Export-ModuleMember -Function Get-DsrKeyMap, Test-DsrRunning, Assert-DsrSaveKnown, Start-Dsr, Get-DsrWindow,
    Send-DsrKey, Get-DsrShot, Wait-DsrStable, Stop-DsrHard, Get-FileSha, Get-DsrState, Wait-DsrState,
    Enter-DsrGame, Exit-DsrToTitle, Close-DsrAtTitle, Invoke-DsrCapture
