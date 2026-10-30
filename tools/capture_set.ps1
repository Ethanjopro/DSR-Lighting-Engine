# Capture A/B sets: for each bonfire, for each variant, install the variant's files, warp there, capture.
# Warping reloads DrawParam from disk, so no quit/continue is needed between variants.
# Requires a running test session (game in gameplay, Gadget hooked). Always restores vanilla files at the end.
#
#   .\tools\capture_set.ps1 -Run baseline -Variants vanilla
#   .\tools\capture_set.ps1 -Run r1 -Variants vanilla,dist -Only firelink,anor_entrance
#
# A variant is "vanilla" or a folder laid out like the game dir (e.g. dist\ or dist\_spike_gray\param\..).
param(
    [Parameter(Mandatory)] [string]$Run,
    [Parameter(Mandatory)] [string[]]$Variants,
    [string[]]$Only,
    [string[]]$Groups
)
$root = Split-Path -Parent $PSScriptRoot
$game = "C:\Program Files (x86)\Steam\steamapps\common\DARK SOULS REMASTERED"
Import-Module "$root\tools\dsr.psm1" -Force
Import-Module "$root\tools\gadget.psm1" -Force

$list = (Get-Content -Raw "$root\data\bonfires.json" | ConvertFrom-Json).bonfires
if ($Only) { $list = $list | Where-Object { $Only -contains $_.slug } }
if ($Groups) { $list = $list | Where-Object { $Groups -contains $_.group } }
if (-not $list) { throw "no bonfires selected" }

function Install-Variant([string]$Variant) {
    & "$root\tools\restore_vanilla.ps1" | Out-Null
    if ($Variant -eq "vanilla") { return }
    $src = if ([IO.Path]::IsPathRooted($Variant)) { $Variant } else { Join-Path $root $Variant }
    foreach ($f in Get-ChildItem $src -Recurse -File) {
        $rel = $f.FullName.Substring($src.Length).TrimStart('\')
        if ($rel -notmatch '^(param\\DrawParam\\a1[0-8]_DrawParam\.parambnd\.dcx|shader\\[^\\]+\.shaderbnd\.dcx)$') {
            throw "variant file outside allowlist: $rel"
        }
        Copy-Item $f.FullName (Join-Path $game $rel) -Force
    }
}

$summary = @()
$current = $null
try {
    foreach ($b in $list) {
        foreach ($v in $Variants) {
            if ($current -ne $v) { Install-Variant $v; $current = $v }
            $vname = Split-Path -Leaf $v.TrimEnd('\')
            $out = Join-Path $root "captures\$Run\$vname"
            $t0 = Get-Date
            try {
                Invoke-GadgetWarp $b.name
                Wait-DsrWarp
                $s = Invoke-DsrCapture -OutDir $out -Name $b.slug
                $summary += [pscustomobject]@{ bonfire = $b.slug; variant = $vname; ev = $s.ev; p50 = $s.p50; clip = $s.clip_pct; crush = $s.crush_pct; sat = $s.mean_sat; secs = [int]((Get-Date) - $t0).TotalSeconds }
                Write-Output ("{0,-20} {1,-10} ev={2,7:N3} p50={3,3} clip={4,6:N2} crush={5,6:N2} sat={6:N3} ({7}s)" -f $b.slug, $vname, $s.ev, $s.p50, $s.clip_pct, $s.crush_pct, $s.mean_sat, [int]((Get-Date) - $t0).TotalSeconds)
            } catch {
                Write-Warning "$($b.slug)/$vname failed: $_"
                $summary += [pscustomobject]@{ bonfire = $b.slug; variant = $vname; error = "$_" }
            }
        }
    }
} finally {
    & "$root\tools\restore_vanilla.ps1" | Out-Null
    $summary | ConvertTo-Json -Depth 3 | Set-Content -Encoding utf8 (Join-Path $root "captures\$Run\summary.json")
}
