# Stability and coverage run: warp through every bonfire with the engine on, capturing each, and report the log's
# timing lines and any errors. Run inside test_session.ps1.
#   .\tools\test_session.ps1 -Script { & .\tools\engine_soak.ps1 -Run soak1 -DrawParam dist\_w4\r3_full }
param(
    [Parameter(Mandatory)] [string]$Run,
    [string]$DrawParam,
    [string[]]$Only,
    [int]$IdleSec = 6
)
$root = Split-Path -Parent $PSScriptRoot
$game = "C:\Program Files (x86)\Steam\steamapps\common\DARK SOULS REMASTERED"
Import-Module "$root\tools\dsr.psm1" -Force
Import-Module "$root\tools\gadget.psm1" -Force

function Invoke-Shot([string]$Path) {
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

$bonfires = (Get-Content -Raw "$root\data\bonfires.json" | ConvertFrom-Json).bonfires
if ($Only) { $bonfires = $bonfires | Where-Object { $Only -contains $_.slug } }

try {
    & "$root\tools\engine_install.ps1" -Remove -KeepAs "$Run\_prev" | Out-Null
    if ($DrawParam) { & "$root\tools\engine_install.ps1" -DrawParam $DrawParam } else { & "$root\tools\engine_install.ps1" }
    Start-Dsr | Out-Null
    Enter-DsrGame
    Start-Gadget | Out-Null
    $ok = 0; $failed = @()
    foreach ($b in $bonfires) {
        if (-not (Test-DsrRunning)) { $failed += "$($b.slug): game gone"; break }
        try {
            try { Invoke-GadgetWarp $b.name } catch { Start-Sleep 3; Invoke-GadgetWarp $b.name }
            Wait-DsrWarp
            Start-Sleep -Seconds $IdleSec
            Invoke-Shot (Join-Path $root "captures\$Run\on\$($b.slug).dds")
            $ok++
            Write-Output "$($b.slug) ok"
        } catch {
            $failed += "$($b.slug): $_"
            Write-Warning "$($b.slug) failed: $_"
        }
    }
    Write-Output "soak: $ok ok, $($failed.Count) failed, game running: $(Test-DsrRunning)"
    $failed | ForEach-Object { Write-Output "  $_" }
} finally {
    Stop-Gadget
    if (Test-DsrRunning) {
        if ((Get-DsrState) -eq "InGame") { Exit-DsrToTitle }
        Close-DsrAtTitle
    }
    for ($i = 0; $i -lt 30 -and (Test-DsrRunning); $i++) { Start-Sleep 1 }
    & "$root\tools\engine_install.ps1" -Remove -KeepAs "$Run\engine_session" | Out-Null
}
