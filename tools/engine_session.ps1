# Engine test run inside the save swap: for each engine variant, restart the game with or without the DLL,
# then warp to the bonfires, capture them, and optionally record a frame trace at each.
#   .\tools\test_session.ps1 -Script { & .\tools\engine_session.ps1 -Run e1 -Variants off,on -Only firelink,parish }
#   -Variants: "off" (no DLL) and "on" (DLL). -Trace "full" or "lite" (lite = no render-target dumps).
param(
    [Parameter(Mandatory)] [string]$Run,
    [string[]]$Variants = @("on"),
    [Parameter(Mandatory)] [string[]]$Only,
    [ValidateSet("none", "full", "lite")] [string]$Trace = "none",
    [int]$TraceTimeoutSec = 180
)
$root = Split-Path -Parent $PSScriptRoot
$game = "C:\Program Files (x86)\Steam\steamapps\common\DARK SOULS REMASTERED"
Import-Module "$root\tools\dsr.psm1" -Force
Import-Module "$root\tools\gadget.psm1" -Force

function Stop-Session {
    Stop-Gadget
    if (Test-DsrRunning) {
        if ((Get-DsrState) -eq "InGame") { Exit-DsrToTitle }
        Close-DsrAtTitle
    }
    for ($i = 0; $i -lt 30 -and (Test-DsrRunning); $i++) { Start-Sleep 1 }
}

# Arm the DLL's tracer and wait until it logs completion. Moves the trace into captures\<Run>\trace\<slug>.
function Invoke-EngineTrace([string]$Slug) {
    $dir = Join-Path $game "dsrle"
    $log = Join-Path $dir "dsrle.log"
    $before = (Select-String -Path $log -Pattern "trace finished" -SimpleMatch).Count
    $mode = if ($Trace -eq "lite") { "n" } else { "f" }
    [IO.File]::WriteAllText((Join-Path $dir "trigger_trace"), $mode)
    $deadline = (Get-Date).AddSeconds($TraceTimeoutSec)
    while ((Select-String -Path $log -Pattern "trace finished" -SimpleMatch).Count -le $before) {
        if ((Get-Date) -gt $deadline) { throw "trace timed out at $Slug" }
        Start-Sleep -Milliseconds 500
    }
    $latest = Get-ChildItem (Join-Path $dir "trace") -Directory | Sort-Object LastWriteTime | Select-Object -Last 1
    $dest = Join-Path $root "captures\$Run\trace\$Slug"
    New-Item -ItemType Directory -Force (Split-Path $dest) | Out-Null
    if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
    Move-Item $latest.FullName $dest
    Write-Output ("trace {0}: {1:N0} MB" -f $Slug, ((Get-ChildItem $dest -File | Measure-Object Length -Sum).Sum / 1MB))
}

$bonfires = (Get-Content -Raw "$root\data\bonfires.json" | ConvertFrom-Json).bonfires | Where-Object { $Only -contains $_.slug }
if (-not $bonfires) { throw "no bonfires selected" }

try {
    foreach ($v in $Variants) {
        Stop-Session
        & "$root\tools\engine_install.ps1" -Remove -KeepAs "$Run\_prev" | Out-Null
        if ($v -eq "on") { & "$root\tools\engine_install.ps1" }
        $t0 = Get-Date
        try {
            Start-Dsr | Out-Null
            Enter-DsrGame
            # A fresh import drops the window handles cached from the previous Gadget process.
            Import-Module "$root\tools\gadget.psm1" -Force
            Start-Gadget | Out-Null
        } catch {
            Write-Warning "$v failed to start: $_"
            $shot = Join-Path $root "captures\$Run\$v`_startfail.png"
            New-Item -ItemType Directory -Force (Split-Path $shot) | Out-Null
            try { Get-DsrShot -Path $shot | Out-Null } catch {}
            Stop-Gadget
            Stop-DsrHard
            continue
        }
        Write-Output ("{0}: game up in {1}s" -f $v, [int]((Get-Date) - $t0).TotalSeconds)
        # Test sessions run with the game's offline start setting; record what the process is connected to.
        $gamePid = (Get-Process -Name DarkSoulsRemastered).Id
        $conns = Get-NetTCPConnection -OwningProcess $gamePid -State Established -ErrorAction SilentlyContinue | Where-Object { $_.RemoteAddress -notmatch '^(127\.|::1)' }
        Write-Output ("network: {0} established non-loopback connection(s) {1}" -f @($conns).Count, (($conns | ForEach-Object { "$($_.RemoteAddress):$($_.RemotePort)" }) -join ' '))
        foreach ($b in $bonfires) {
            try {
                try { Invoke-GadgetWarp $b.name } catch { Write-Warning "$($b.slug): warp retry after: $_"; Start-Sleep 3; Invoke-GadgetWarp $b.name }
                Wait-DsrWarp
                $s = Invoke-DsrCapture -OutDir (Join-Path $root "captures\$Run\$v") -Name $b.slug
                Write-Output ("{0,-20} {1,-4} ev={2,7:N3} p50={3,3} clip={4,6:N2} crush={5,6:N2} sat={6:N3}" -f $b.slug, $v, $s.ev, $s.p50, $s.clip_pct, $s.crush_pct, $s.mean_sat)
                if ($v -eq "on" -and $Trace -ne "none") { Invoke-EngineTrace $b.slug }
            } catch {
                Write-Warning "$($b.slug)/$v failed: $_"
            }
        }
        Stop-Session
        & "$root\tools\engine_install.ps1" -Remove -KeepAs "$Run\$v" | Out-Null
    }
} finally {
    Stop-Session
    & "$root\tools\engine_install.ps1" -Remove -KeepAs "$Run\_final" | Out-Null
}
