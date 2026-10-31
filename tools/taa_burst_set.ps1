# Burst captures for anti-aliasing A/Bs: per bonfire, frames while standing still (temporal flicker, sharpness)
# and frames during a constant-rate camera pan (detail energy in motion), saved as still_NN.png / pan_NN.png.
# Requires a running test session (game in gameplay, Gadget hooked). Analyse with tools/taa_burst_report.py.
#
#   .\tools\taa_burst_set.ps1 -Run s6\burst\vanilla -Only archives_balcony,anor_entrance
param(
    [Parameter(Mandatory)] [string]$Run,
    [Parameter(Mandatory)] [string[]]$Only,
    [int]$StillFrames = 10,
    [int]$PanFrames = 10,
    [int]$PanDx = 4,
    [int]$PanStepMs = 10
)
$root = Split-Path -Parent $PSScriptRoot
Import-Module "$root\tools\dsr.psm1" -Force
Import-Module "$root\tools\gadget.psm1" -Force
if (-not ("DsrMouse" -as [type])) { Add-Type -TypeDefinition (Get-Content -Raw "$root\tools\input\DsrMouse.cs") }

$list = (Get-Content -Raw "$root\data\bonfires.json" | ConvertFrom-Json).bonfires | Where-Object { $Only -contains $_.slug }
foreach ($b in $list) {
    $out = Join-Path $root "captures\$Run\$($b.slug)"
    New-Item -ItemType Directory -Force $out | Out-Null
    try {
        try { Invoke-GadgetWarp $b.name } catch { Start-Sleep 3; Invoke-GadgetWarp $b.name }
        Wait-DsrWarp
        $h = Get-DsrWindow
        [void][DsrInput]::Focus($h)
        Start-Sleep -Seconds 3     # let the sharpening settle (it builds up over ~0.5-2 s when standing still)
        $still = @()
        for ($i = 0; $i -lt $StillFrames; $i++) { $still += [DsrInput]::Grab($h, "screen"); Start-Sleep -Milliseconds 150 }
        [DsrMouse]::StartPan($PanDx, $PanStepMs)
        $t0 = Get-Date
        Start-Sleep -Milliseconds 700  # reach a steady pan
        $pan = @()
        for ($i = 0; $i -lt $PanFrames; $i++) { $pan += [DsrInput]::Grab($h, "screen") }
        [DsrMouse]::StopPan()
        $ms = [int]((Get-Date) - $t0).TotalMilliseconds
        [DsrMouse]::StartPan(-$PanDx, $PanStepMs); Start-Sleep -Milliseconds $ms; [DsrMouse]::StopPan()   # turn back
        for ($i = 0; $i -lt $still.Count; $i++) { [DsrInput]::Save($still[$i], (Join-Path $out ("still_{0:D2}.png" -f $i))); $still[$i].Dispose() }
        for ($i = 0; $i -lt $pan.Count; $i++) { [DsrInput]::Save($pan[$i], (Join-Path $out ("pan_{0:D2}.png" -f $i))); $pan[$i].Dispose() }
        Write-Output ("burst {0,-20} still {1} pan {2} ({3} ms pan)" -f $b.slug, $StillFrames, $PanFrames, $ms)
    } catch {
        [DsrMouse]::StopPan()
        Write-Warning "burst $($b.slug) failed: $_"
    }
}
