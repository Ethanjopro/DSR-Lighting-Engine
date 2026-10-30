# DSR Lighting Engine installer. Copies the mod's loose visual files into the game folder after backing up the
# files they replace. Only DrawParam a10..a18 and DX11 shader archives are ever touched (allowlist below).
#
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-GameDir "D:\Steam\steamapps\common\DARK SOULS REMASTERED"] [-Force]
#
# -Force: also replace files that are neither vanilla nor this mod (another mod's DrawParam or shaders). They are
#         backed up as they are and put back by uninstall.ps1.
param([string]$GameDir, [switch]$Force)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$allow = '^(param/DrawParam/a1[0-8]_DrawParam\.parambnd\.dcx|shader/FRPG_[A-Za-z]+(_fpo)?_DX11\.shaderbnd\.dcx)$'

. (Join-Path $here "common.ps1")
$GameDir = Resolve-GameDir $GameDir

$manifest = Get-Content -Raw (Join-Path $here "manifest.json") | ConvertFrom-Json
$backup = Join-Path $GameDir "DSRLE_backup"
$record = Join-Path $backup "install.json"
$state = @{}
if (Test-Path $record) {
    foreach ($e in (Get-Content -Raw $record | ConvertFrom-Json).files) { $state[$e.path] = $e }
}

# Check everything before changing anything.
$plan = @()
foreach ($f in $manifest.files) {
    if ($f.path -notmatch $allow) { throw "manifest entry outside the allowlist: $($f.path)" }
    $src = Join-Path $here ("files\" + ($f.path -replace '/', '\'))
    if ((Get-Sha $src) -ne $f.sha256) { throw "mod file is damaged (hash mismatch): $($f.path). Re-download the mod." }
    $dst = Join-Path $GameDir ($f.path -replace '/', '\')
    $cur = if (Test-Path $dst) { Get-Sha $dst } else { "" }
    if ($cur -eq $f.sha256) { $action = "current" }
    elseif ($state.ContainsKey($f.path)) { $action = "update" }        # an older DSRLE version: keep the first backup
    elseif ($cur -eq $f.vanilla_sha256) { $action = "backup" }
    elseif ($Force) { $action = "backup-foreign" }
    else { throw "$($f.path) is neither vanilla nor DSRLE (another mod?). Uninstall that mod first, or rerun with -Force." }
    $plan += [pscustomobject]@{ f = $f; src = $src; dst = $dst; cur = $cur; action = $action }
}

New-Item -ItemType Directory -Force $backup | Out-Null
foreach ($p in $plan) {
    if ($p.action -eq "current") { Write-Output "up to date  $($p.f.path)"; continue }
    if ($p.action -like "backup*") {
        $bak = Join-Path $backup ($p.f.path -replace '/', '\')
        New-Item -ItemType Directory -Force (Split-Path $bak) | Out-Null
        if ($p.cur) { Copy-Item $p.dst $bak -Force }
        $state[$p.f.path] = [pscustomobject]@{ path = $p.f.path; backup_sha256 = $p.cur; foreign = ($p.action -eq "backup-foreign") }
    }
    New-Item -ItemType Directory -Force (Split-Path $p.dst) | Out-Null
    Copy-Item $p.src $p.dst -Force
    if ((Get-Sha $p.dst) -ne $p.f.sha256) { throw "copy failed for $($p.f.path); run uninstall.ps1 to restore." }
    Write-Output "installed   $($p.f.path)"
    # Save the record after every file, so an interrupted install can still be uninstalled.
    [pscustomobject]@{ mod = $manifest.name; version = $manifest.version; files = @($state.Values) } |
        ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 $record
}
[pscustomobject]@{ mod = $manifest.name; version = $manifest.version; files = @($state.Values) } |
    ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 $record
Write-Output "DSR Lighting Engine $($manifest.version) installed in $GameDir (backups in DSRLE_backup)."
