# DSR Lighting Engine installer. Copies the mod's loose visual files into the game folder after backing up the
# files they replace. Only DrawParam a10..a18 and the two DX11 shader archives below are ever touched (allowlist).
#
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-GameDir "D:\Steam\steamapps\common\DARK SOULS REMASTERED"] [-Force]
#
# -Force: also replace files that are neither vanilla nor this mod (another mod's DrawParam or shaders). They are
#         backed up as they are and put back by uninstall.ps1.
[CmdletBinding()]
param([string]$GameDir, [switch]$Force)
$ErrorActionPreference = "Stop"
trap { [Console]::Error.WriteLine("ERROR: " + $_.Exception.Message); exit 1 }
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

. (Join-Path $here "common.ps1")
$GameDir = Resolve-GameDir $GameDir

$manifest = Get-Content -Raw -LiteralPath (Join-Path $here "manifest.json") | ConvertFrom-Json
$backup = Join-Path $GameDir "DSRLE_backup"
$record = Join-Path $backup "install.json"
$state = @{}
if (Test-Path -LiteralPath $record) {
    foreach ($e in (Get-Content -Raw -LiteralPath $record | ConvertFrom-Json).files) { $state[$e.path] = $e }
}

# Check everything before changing anything.
$plan = @()
$seen = @{}
foreach ($f in $manifest.files) {
    if ($f.path -cnotmatch $DsrleAllow) { throw "manifest entry outside the allowlist: $($f.path)" }
    if ($seen.ContainsKey($f.path.ToLowerInvariant())) { throw "duplicate manifest entry: $($f.path)" }
    $seen[$f.path.ToLowerInvariant()] = $true
    $src = Join-Path $here ("files\" + ($f.path -replace '/', '\'))
    if ((Get-Sha $src) -ne $f.sha256) { throw "mod file is damaged (hash mismatch): $($f.path). Re-download the mod." }
    $dst = Join-Path $GameDir ($f.path -replace '/', '\')
    $bak = Join-Path $backup ($f.path -replace '/', '\')
    $cur = if (Test-Path -LiteralPath $dst) { Get-Sha $dst } else { "" }
    $e = $state[$f.path]
    if ($e -and $cur -eq $f.sha256) { $action = "current" }
    elseif ($cur -eq $f.sha256) {
        # DSRLE's file but no record of what it replaced (DSRLE_backup was deleted?). Never guess: the original would be lost.
        if ((Test-Path -LiteralPath $bak) -and (Get-Sha $bak) -eq $f.vanilla_sha256) { $action = "adopt" }
        else { throw "$($f.path) is already the DSRLE version, but DSRLE_backup has no record of the original. Use Steam > Dark Souls Remastered > Properties > Installed Files > Verify integrity of game files, then run install.ps1 again." }
    }
    elseif ($e) {
        # An older DSRLE version, an interrupted install, or Steam put the vanilla file back: keep the first backup.
        # Anything else was written by someone else after DSRLE: do not overwrite it silently.
        if ($e.installed_sha256 -and $e.status -ne "pending" -and $cur -ne $e.installed_sha256 -and $cur -ne $e.backup_sha256 -and $cur -ne $f.vanilla_sha256 -and -not $Force) {
            throw "$($f.path) was replaced by something else after DSRLE was installed (another mod?). Uninstall that mod first, or rerun with -Force."
        }
        $action = "update"
    }
    elseif ($cur -eq $f.vanilla_sha256) { $action = "backup" }
    elseif ($cur -eq "") { $action = "add" }                  # file missing from the game folder: nothing to back up
    elseif ($Force) { $action = "backup-foreign" }
    else { throw "$($f.path) is neither vanilla nor DSRLE (another mod?). Uninstall that mod first, or rerun with -Force." }
    $plan += [pscustomobject]@{ f = $f; src = $src; dst = $dst; bak = $bak; cur = $cur; action = $action }
}

# Files an older DSRLE version installed that this version no longer ships: put the original back.
$dropped = @($state.Keys | Where-Object { -not $seen.ContainsKey($_.ToLowerInvariant()) })
foreach ($k in $dropped) {
    $e = $state[$k]
    if ($k -cnotmatch $DsrleAllow) { throw "unexpected entry in install.json: $k" }
    $dst = Join-Path $GameDir ($k -replace '/', '\'); $bak = Join-Path $backup ($k -replace '/', '\')
    $cur = if (Test-Path -LiteralPath $dst) { Get-Sha $dst } else { "" }
    if ($e.installed_sha256 -and $e.status -ne "pending" -and $cur -ne $e.installed_sha256 -and $cur -ne $e.backup_sha256) {
        Write-Warning "$k (no longer part of DSRLE) was changed by something else; left alone, original kept in DSRLE_backup."; continue
    }
    if (-not $e.backup_sha256) { if ($cur) { Remove-Item -LiteralPath $dst -Force } }
    elseif ((Test-Path -LiteralPath $bak) -and (Get-Sha $bak) -eq $e.backup_sha256) {
        Copy-Item -LiteralPath $bak $dst -Force
        if ((Get-Sha $dst) -ne $e.backup_sha256) { throw "could not restore $k" }
        Remove-Item -LiteralPath $bak -Force
    } else { Write-Warning "$k is no longer part of DSRLE but its backup is missing; use Steam's 'Verify integrity of game files'."; continue }
    $state.Remove($k)
    Write-Output "restored    $k (no longer part of DSRLE)"
}

New-Item -ItemType Directory -Force $backup | Out-Null
Save-Record $record $manifest $state
foreach ($p in $plan) {
    $f = $p.f
    if ($p.action -eq "current") {
        $e = $state[$f.path]
        if ($e.status -ne "installed") { $state[$f.path] = New-Entry $f.path $e.backup_sha256 ([bool]$e.foreign) $f.sha256 "installed" }   # record from an older installer, or an install that was interrupted after this copy
        Write-Output "up to date  $($f.path)"; continue
    }
    New-Item -ItemType Directory -Force (Split-Path $p.bak) | Out-Null
    switch -Wildcard ($p.action) {
        "backup*" {
            Copy-Item -LiteralPath $p.dst $p.bak -Force
            if ((Get-Sha $p.bak) -ne $p.cur) { throw "backup failed for $($f.path); nothing was changed for this file." }
            $state[$f.path] = New-Entry $f.path $p.cur ($p.action -eq "backup-foreign") $f.sha256 "pending"
        }
        "add"   { $state[$f.path] = New-Entry $f.path "" $false $f.sha256 "pending" }
        "adopt" { $state[$f.path] = New-Entry $f.path $f.vanilla_sha256 $false $f.sha256 "pending" }
        "update" {
            $old = $state[$f.path]
            if ($p.cur -and $p.cur -ne $old.installed_sha256 -and $p.cur -ne $old.backup_sha256 -and $p.cur -ne $f.vanilla_sha256) {
                Copy-Item -LiteralPath $p.dst ($p.bak + ".replaced-" + $p.cur.Substring(0, 8)) -Force   # -Force over another mod: keep its file
            }
            $state[$f.path] = New-Entry $f.path $old.backup_sha256 ([bool]$old.foreign) $f.sha256 "pending"
        }
    }
    Save-Record $record $manifest $state          # BEFORE the overwrite: an interruption from here on is uninstallable
    Copy-Item -LiteralPath $p.src $p.dst -Force
    if ((Get-Sha $p.dst) -ne $f.sha256) { throw "copy failed for $($f.path); run uninstall.ps1 to restore." }
    $e = $state[$f.path]
    $state[$f.path] = New-Entry $f.path $e.backup_sha256 ([bool]$e.foreign) $f.sha256 "installed"
    Save-Record $record $manifest $state
    Write-Output "installed   $($f.path)"
}
Save-Record $record $manifest $state
Write-Output "DSR Lighting Engine $($manifest.version) installed in $GameDir (backups in DSRLE_backup)."
