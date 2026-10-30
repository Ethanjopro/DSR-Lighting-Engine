# Shader A/B: shaders are only read at startup, so each variant needs a full game restart.
# For each variant: quit to title, close, install the variant's shader files, relaunch, load in, re-hook Gadget,
# then capture the given bonfires via capture_set.ps1. Vanilla files are restored at the end (game left running).
#
#   .\tools\shader_session.ps1 -Run shader1 -Variants dist\_shader\rebuild0,dist\_shader\gentint -Only firelink,parish
# -CaptureVariants: capture these folders (e.g. shader + DrawParam run folders) instead of the shader variant
# itself; capture_set cycles them by warping within the one restart (see dist\_probe\capture_plan.ps1).
param(
    [Parameter(Mandatory)] [string]$Run,
    [Parameter(Mandatory)] [string[]]$Variants,
    [Parameter(Mandatory)] [string[]]$Only,
    [string[]]$CaptureVariants
)
$root = Split-Path -Parent $PSScriptRoot
$game = "C:\Program Files (x86)\Steam\steamapps\common\DARK SOULS REMASTERED"
Import-Module "$root\tools\dsr.psm1" -Force
Import-Module "$root\tools\gadget.psm1" -Force

function Restart-Dsr([string]$Variant) {
    Stop-Gadget
    if (Test-DsrRunning) {
        if ((Get-DsrState) -eq "InGame") { Exit-DsrToTitle }
        Close-DsrAtTitle
    }
    & "$root\tools\restore_vanilla.ps1" | Out-Null
    if ($Variant -ne "vanilla") {
        $src = Join-Path $root $Variant
        foreach ($f in Get-ChildItem (Join-Path $src "shader") -File -Filter *.shaderbnd.dcx) {
            Copy-Item $f.FullName (Join-Path $game "shader\$($f.Name)") -Force
            Write-Output "installed $($f.Name) from $Variant"
        }
    }
    Start-Dsr | Out-Null
    Enter-DsrGame
    Start-Gadget | Out-Null
}

try {
    foreach ($v in $Variants) {
        $t0 = Get-Date
        try {
            Restart-Dsr $v
            Write-Output ("{0}: game up in {1}s" -f $v, [int]((Get-Date) - $t0).TotalSeconds)
        } catch {
            Write-Warning "$v failed to start: $_"
            $shot = Join-Path $root "captures\$Run\$(Split-Path -Leaf $v)_startfail.png"
            New-Item -ItemType Directory -Force (Split-Path $shot) | Out-Null
            try { Get-DsrShot -Path $shot | Out-Null } catch {}
            Stop-Gadget; Stop-DsrHard
            continue
        }
        # capture_set reinstalls the same files while running (harmless: shaders are not re-read) and restores at the end.
        $cap = if ($CaptureVariants) { $CaptureVariants } else { @($v) }
        & "$root\tools\capture_set.ps1" -Run "$Run\$(Split-Path -Leaf $v)" -Variants $cap -Only $Only
    }
} finally {
    & "$root\tools\restore_vanilla.ps1" | Out-Null
}
