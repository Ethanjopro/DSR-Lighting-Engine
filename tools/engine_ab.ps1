# Engine A/B inside one game session: settings and HLSL reload live, so no restart between variants.
# Each variant is an ini in engine\variants\<name>.ini; "off" and "on" need no file. Run inside test_session.ps1.
#   .\tools\test_session.ps1 -Script { & .\tools\engine_ab.ps1 -Run ab1 -Variants off,on -Only burg,catacombs }
# Shots are the game's exact back buffer, with the HUD hidden. Desktop capture is no use here: this machine's
# desktop is HDR with Auto HDR on the game, so GDI returns the compositor's clipped, brightened version.
param(
    [Parameter(Mandatory)] [string]$Run,
    [string[]]$Variants = @("off", "on"),
    [Parameter(Mandatory)] [string[]]$Only,
    [ValidateSet("none", "full", "lite")] [string]$Trace = "none",
    [string]$DrawParam,     # a dist folder with the per-area DrawParam build to use for the whole session
    [switch]$VanillaOff,    # the "off" variant gets vanilla DrawParam too: the game as a player knows it
    [switch]$TestToggle,    # after the last bonfire, press the comparison key and check the log
    [int[]]$Turns = @(),    # also capture a view after each of these camera turns, in mouse travel; saved as <slug>_t1, _t2...
    [int]$SettleSec = 5,    # let AO, TAA and exposure settle after a settings change
    [switch]$Hdr,           # start the game with native HDR; the shots are then scRGB, so read them with dds2png.shot_image
    [switch]$ShowUi,        # keep the HUD in the shots
    [switch]$Maps           # keep the per-map values; without it every variant runs with [maps] enabled = 0
)
$root = Split-Path -Parent $PSScriptRoot
$game = "C:\Program Files (x86)\Steam\steamapps\common\DARK SOULS REMASTERED"
Import-Module "$root\tools\dsr.psm1" -Force
Import-Module "$root\tools\gadget.psm1" -Force

function Set-Variant([string]$Name) {
    $ini = Join-Path $game "dsrle\dsrle.ini"
    $hide = if ($ShowUi) { "0" } else { "1" }
    if ($Name -eq "off") { $text = "[engine]`nenabled = 0`nhide_ui = $hide`n" }
    elseif ($Name -eq "on") { $text = (Get-Content -Raw "$root\engine\data\dsrle.ini") + "`n[engine]`nhide_ui = $hide`n" }
    else {
        # A variant is the base settings plus its own overrides; a later value for a key wins.
        $text = (Get-Content -Raw "$root\engine\data\dsrle.ini") + "`n" + (Get-Content -Raw "$root\engine\variants\$Name.ini") + "`n[engine]`nhide_ui = $hide`n"
    }
    # A per-map value beats the global key, so a variant's global value would not show in those maps.
    if (-not $Maps) { $text += "[maps]`nenabled = 0`n" }
    [IO.File]::WriteAllText($ini, $text)
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

function Invoke-CameraTurn([int]$Travel) {
    [void][DsrInput]::Focus((Get-DsrWindow))
    [DsrInput]::MoveMouse($Travel, 0, 60, 16)
    Start-Sleep -Seconds 1
}

function Invoke-EngineTrace([string]$Slug) {
    $dir = Join-Path $game "dsrle"
    $log = Join-Path $dir "dsrle.log"
    $before = (Select-String -Path $log -Pattern "trace finished" -SimpleMatch).Count
    $mode = if ($Trace -eq "lite") { "n" } else { "f" }
    [IO.File]::WriteAllText((Join-Path $dir "trigger_trace"), $mode)
    $deadline = (Get-Date).AddSeconds(180)
    while ((Select-String -Path $log -Pattern "trace finished" -SimpleMatch).Count -le $before) {
        if ((Get-Date) -gt $deadline) { throw "trace timed out at $Slug" }
        Start-Sleep -Milliseconds 500
    }
    $latest = Get-ChildItem (Join-Path $dir "trace") -Directory | Sort-Object LastWriteTime | Select-Object -Last 1
    $dest = Join-Path $root "captures\$Run\trace\$Slug"
    New-Item -ItemType Directory -Force (Split-Path $dest) | Out-Null
    if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
    Move-Item $latest.FullName $dest
}

$bonfires = (Get-Content -Raw "$root\data\bonfires.json" | ConvertFrom-Json).bonfires | Where-Object { $Only -contains $_.slug }
if (-not $bonfires) { throw "no bonfires selected" }
if ($Maps) { Write-Output "per-map values: kept (-Maps), [maps] as dsrle.ini and each variant set it" }
else { Write-Output "per-map values: off in every variant ([maps] enabled = 0); -Maps keeps them" }

try {
    & "$root\tools\engine_install.ps1" -Remove -KeepAs "$Run\_prev" | Out-Null
    if ($DrawParam) { & "$root\tools\engine_install.ps1" -DrawParam $DrawParam -Hdr:$Hdr } else { & "$root\tools\engine_install.ps1" -Hdr:$Hdr }
    Start-Dsr | Out-Null
    Enter-DsrGame
    Start-Gadget | Out-Null
    Write-Output ("network: {0} established non-loopback connection(s)" -f @(Get-NetTCPConnection -OwningProcess (Get-Process -Name DarkSoulsRemastered).Id -State Established -ErrorAction SilentlyContinue | Where-Object { $_.RemoteAddress -notmatch '^(127\.|::1)' }).Count)
    foreach ($b in $bonfires) {
        try {
            try { Invoke-GadgetWarp $b.name } catch { Write-Warning "$($b.slug): warp retry after: $_"; Start-Sleep 3; Invoke-GadgetWarp $b.name }
            Wait-DsrWarp
            foreach ($v in $Variants) {
                if ($VanillaOff -and $DrawParam) {
                    # DrawParam loads on warp, so the vanilla "before" costs a warp each way.
                    if ($v -eq "off") {
                        & "$root\tools\restore_vanilla.ps1" -DrawParamOnly | Out-Null
                    } else {
                        foreach ($f in Get-ChildItem (Join-Path $root "$DrawParam\param\DrawParam") -File -Filter *.parambnd.dcx) {
                            Copy-Item $f.FullName (Join-Path $game "param\DrawParam\$($f.Name)") -Force
                        }
                    }
                    [IO.File]::WriteAllText((Join-Path $game "dsrle\dsrle.ini"), "[engine]`nenabled = 0`n")
                    Start-Sleep -Seconds 1
                    Invoke-GadgetWarp $b.name
                    Wait-DsrWarp
                }
                Set-Variant $v
                Start-Sleep -Seconds $SettleSec
                Invoke-EngineShot (Join-Path $root "captures\$Run\$v\$($b.slug).dds")
                Write-Output "$($b.slug) $v captured"
                if ($VanillaOff -and $DrawParam) {
                    # The warp put the camera back, so each variant walks the same turns.
                    for ($t = 0; $t -lt $Turns.Count; $t++) {
                        Invoke-CameraTurn $Turns[$t]
                        Start-Sleep -Seconds $SettleSec
                        Invoke-EngineShot (Join-Path $root "captures\$Run\$v\$($b.slug)_t$($t + 1).dds")
                    }
                }
            }
            if (-not ($VanillaOff -and $DrawParam)) {
                for ($t = 0; $t -lt $Turns.Count; $t++) {
                    Invoke-CameraTurn $Turns[$t]
                    foreach ($v in $Variants) {
                        Set-Variant $v
                        Start-Sleep -Seconds $SettleSec
                        Invoke-EngineShot (Join-Path $root "captures\$Run\$v\$($b.slug)_t$($t + 1).dds")
                    }
                }
            }
            if ($Trace -ne "none") { Invoke-EngineTrace $b.slug }
        } catch {
            Write-Warning "$($b.slug) failed: $_"
        }
        # The harness detects gameplay from HUD pixels, so the HUD must be back before the next warp.
        [IO.File]::WriteAllText((Join-Path $game "dsrle\dsrle.ini"), "[engine]`nenabled = 0`n")
        Start-Sleep -Seconds 2
    }
    if ($TestToggle) {
        Copy-Item "$root\engine\data\dsrle.ini" (Join-Path $game "dsrle\dsrle.ini") -Force
        Start-Sleep -Seconds 2
        $log = Join-Path $game "dsrle\dsrle.log"
        $before = (Select-String -Path $log -Pattern "engine: toggled" -SimpleMatch).Count
        [void][DsrInput]::Focus((Get-DsrWindow))
        [DsrInput]::Tap(0x44, 120)    # scan code for F10
        Start-Sleep -Seconds 2
        [DsrInput]::Tap(0x44, 120)
        Start-Sleep -Seconds 2
        $after = (Select-String -Path $log -Pattern "engine: toggled" -SimpleMatch).Count
        Write-Output "toggle key: $($after - $before) toggles logged (expected 2)"
    }
} finally {
    Stop-Gadget
    if (Test-DsrRunning) {
        [IO.File]::WriteAllText((Join-Path $game "dsrle\dsrle.ini"), "[engine]`nenabled = 0`n")
        Start-Sleep -Seconds 2
        if ((Get-DsrState) -eq "InGame") { Exit-DsrToTitle }
        Close-DsrAtTitle
    }
    for ($i = 0; $i -lt 30 -and (Test-DsrRunning); $i++) { Start-Sleep 1 }
    & "$root\tools\engine_install.ps1" -Remove -KeepAs "$Run\engine_session" | Out-Null
}
