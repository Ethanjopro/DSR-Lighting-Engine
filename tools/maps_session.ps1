# Per-map detection session inside the save swap. The engine runs with [maps] apply = 0 and log = 1, so it logs the
# map it finds without changing a pixel. At each bonfire the log is marked on arrival and again after a full turn.
#   .\tools\test_session.ps1 -Script { & .\tools\maps_session.ps1 -Run m1 }
#   .\tools\test_session.ps1 -Script { & .\tools\maps_session.ps1 -Run m2 -Only depths,burg -Shots }
#   .\tools\test_session.ps1 -Script { & .\tools\maps_session.ps1 -Run m3 -Only burg -Walk }
# With -Walk, the bonfires are only the start: you then walk the borders yourself and type a label in this window at
# each one (Lower Burg to the Depths door, the Depths' shared-light areas, the Depths to Blighttown, the Burg to
# Darkroot, Garden to Basin, Basin to the Valley of Drakes, Sen's to Anor Londo, Gwynevere's stairs, the Manus portal).
# The log is copied to captures\<Run>\maps.log. engine\data\maps_detect.txt must come from tools\maps_table.py --emit
# on the same DrawParam build, or the engine turns detection off.
param(
    [Parameter(Mandatory)] [string]$Run,
    [string[]]$Only,                      # bonfire slugs; every bonfire in data\bonfires.json when left out
    [string]$DrawParam = "dist\_v4\sun",  # the DrawParam build maps_detect.txt was made from
    [int]$StillSec = 3,                   # standing still after the arrival mark
    [int]$TurnTravel = 8000,              # mouse travel for about one full turn, made in four quarters
    [switch]$Shots,                       # also save one back-buffer shot per bonfire, before the turn; the HUD stays in it
    [switch]$Walk                         # after the bonfires, mark a walk you make yourself; an empty label ends it
)
$root = Split-Path -Parent $PSScriptRoot
$game = "C:\Program Files (x86)\Steam\steamapps\common\DARK SOULS REMASTERED"
Import-Module "$root\tools\dsr.psm1" -Force
Import-Module "$root\tools\gadget.psm1" -Force

# The engine logs the label with the present it reached and then deletes the file, as it does with trigger_shot.
function Invoke-MapsMark([string]$Label) {
    $trigger = Join-Path $game "dsrle\trigger_mark"
    [IO.File]::WriteAllText($trigger, $Label)
    $deadline = (Get-Date).AddSeconds(10)
    while (Test-Path $trigger) {
        if ((Get-Date) -gt $deadline) {
            # Left in place, the label would be logged later at another view, or overwritten by the next one.
            Remove-Item $trigger -Force -ErrorAction SilentlyContinue
            Write-Warning "mark $Label was not taken by the engine"
            return $false
        }
        Start-Sleep -Milliseconds 100
    }
    return $true
}

function Invoke-EngineShot([string]$Path) {
    New-Item -ItemType Directory -Force (Split-Path $Path) | Out-Null
    if (Test-Path $Path) { Remove-Item $Path -Force }
    [IO.File]::WriteAllText((Join-Path $game "dsrle\trigger_shot"), $Path)
    $deadline = (Get-Date).AddSeconds(20)
    while (-not (Test-Path $Path)) {
        if ((Get-Date) -gt $deadline) { throw "shot timed out: $Path" }
        Start-Sleep -Milliseconds 100
    }
    Start-Sleep -Milliseconds 500
}

# engine_ab's -Turns 2000 turned the f4 catacombs view about a quarter of the way round, so the default full turn is an
# estimate. Going past 360 degrees only sees views again. Each quarter moves like engine_ab's turn and is held a second.
function Invoke-FullTurn {
    [void][DsrInput]::Focus((Get-DsrWindow))
    for ($q = 0; $q -lt 4; $q++) {
        [DsrInput]::MoveMouse([int]($TurnTravel / 4), 0, 60, 16)
        Start-Sleep -Seconds 1
    }
}

$bonfires = (Get-Content -Raw "$root\data\bonfires.json" | ConvertFrom-Json).bonfires
if ($Only) { $bonfires = $bonfires | Where-Object { $Only -contains $_.slug } }
if (-not $bonfires) { throw "no bonfires selected" }
if (-not (Test-Path "$root\engine\data\maps_detect.txt")) { throw "no engine\data\maps_detect.txt: run tools\maps_table.py --emit first" }
$log = Join-Path $game "dsrle\dsrle.log"
$written = 0
$planned = 2 * @($bonfires).Count

try {
    & "$root\tools\engine_install.ps1" -Remove -KeepAs "$Run\_prev" | Out-Null
    & "$root\tools\engine_install.ps1" -DrawParam $DrawParam
    # The installed copy is engine\data\dsrle.ini plus the install's [hdr] line, and both must be there when the game
    # starts. The HUD stays, because the harness detects gameplay from it, so the engine stays on through the warps.
    [IO.File]::AppendAllText((Join-Path $game "dsrle\dsrle.ini"), "`n[maps]`nenabled = 1`napply = 0`nlog = 1`n[engine]`nhide_ui = 0`n")
    Start-Dsr | Out-Null
    Enter-DsrGame
    Start-Gadget | Out-Null
    Write-Output ("network: {0} established non-loopback connection(s)" -f @(Get-NetTCPConnection -OwningProcess (Get-Process -Name DarkSoulsRemastered).Id -State Established -ErrorAction SilentlyContinue | Where-Object { $_.RemoteAddress -notmatch '^(127\.|::1)' }).Count)
    $table = Select-String -Path $log -Pattern "maps: (no )?table" | Select-Object -First 1
    if ($table) { Write-Output $table.Line } else { Write-Warning "the log has no maps table line" }
    foreach ($b in $bonfires) {
        try {
            try { Invoke-GadgetWarp $b.name } catch { Write-Warning "$($b.slug): warp retry after: $_"; Start-Sleep 3; Invoke-GadgetWarp $b.name }
            Wait-DsrWarp
            if (Invoke-MapsMark $b.slug) { $written++ }
            Start-Sleep -Seconds $StillSec
            if ($Shots) { Invoke-EngineShot (Join-Path $root "captures\$Run\$($b.slug).dds") }
            Invoke-FullTurn
            if (Invoke-MapsMark "$($b.slug)_turned") { $written++ }
            Write-Output "$($b.slug) marked"
        } catch {
            Write-Warning "$($b.slug) failed: $_"
        }
    }
    if ($Walk) {
        Write-Output "walk: play to each border, then type its label here and press Enter; an empty label ends the walk"
        while ($label = "$(Read-Host 'mark')".Trim() -replace '\s+', '_') {
            # Back to the game first: the engine takes the mark at its next frames, of the view the walk left.
            [void][DsrInput]::Focus((Get-DsrWindow))
            if (Invoke-MapsMark $label) { $written++ }
            $planned++
        }
    }
} finally {
    Stop-Gadget
    if (Test-DsrRunning) {
        [IO.File]::WriteAllText((Join-Path $game "dsrle\dsrle.ini"), "[engine]`nenabled = 0`n")
        Start-Sleep -Seconds 2
        # A throw here would skip the log copy below, which is this run's result.
        try {
            if ((Get-DsrState) -eq "InGame") { Exit-DsrToTitle }
            Close-DsrAtTitle
        } catch {
            Write-Warning "clean exit failed ($_); closing the game"
            Stop-DsrHard
        }
    }
    for ($i = 0; $i -lt 30 -and (Test-DsrRunning); $i++) { Start-Sleep 1 }
    # Copied before the removal below moves the dsrle folder, and also after a failed run.
    if (Test-Path $log) {
        New-Item -ItemType Directory -Force (Join-Path $root "captures\$Run") | Out-Null
        Copy-Item $log (Join-Path $root "captures\$Run\maps.log") -Force
        $logged = @(Select-String -Path $log -Pattern "maps: mark " -SimpleMatch).Count
        Write-Output "maps.log: $logged marks logged, $written taken by the engine, $planned planned"
    }
    & "$root\tools\engine_install.ps1" -Remove -KeepAs "$Run\engine_session" | Out-Null
}
