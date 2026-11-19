# DSR Lighting Engine installer. Puts d3d11.dll and the dsrle folder in the game folder and replaces DrawParam a10..a18
# after backing them up. Nothing outside the allowlist in common.ps1 is ever touched. The shader archives are only read,
# except that DSRLE 1.0's archives are put back from 1.0's own backup: the engine needs the vanilla ones.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-GameDir "D:\Steam\steamapps\common\DARK SOULS REMASTERED"] [-Force]
#
# -Force: also replace a d3d11.dll or DrawParam file that is neither vanilla nor this mod's (another mod). They are
#         backed up as they are and put back by uninstall.ps1; that backup takes the place of any earlier one. The one
#         exception: a DrawParam file another mod wrote over this one when the backup holds the vanilla file. It is
#         overwritten, and the vanilla file stays the backup. With a shader archive that is not vanilla, -Force installs
#         anyway and leaves the archive alone; the shader replacements then do not apply.
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
Restore-Record $record
if (Test-Path -LiteralPath $record) {
    foreach ($e in @(Read-Record $record)) { $state[$e.path] = $e }
}
function In-Game([string]$Rel) { Join-Path $GameDir ($Rel -replace '/', '\') }
function In-Backup([string]$Rel) { Join-Path $backup ($Rel -replace '/', '\') }

# ---- Check everything before changing anything. ----
$dataDir = In-Game $DsrleDir
if ((Test-Path -LiteralPath $dataDir) -and -not (Test-Path -LiteralPath $dataDir -PathType Container)) {
    throw "$dataDir is a file; the engine needs a folder with that name. Nothing was changed."
}

$plan = @()
$seen = @{ $DsrleIniDefault.ToLowerInvariant() = $true }      # written beside dsrle.ini below, not a manifest entry
foreach ($f in $manifest.files) {
    if ($f.path -cnotmatch $DsrleAllow) { throw "manifest entry outside the allowlist: $($f.path)" }
    if ($seen.ContainsKey($f.path.ToLowerInvariant())) { throw "duplicate manifest entry: $($f.path)" }
    $seen[$f.path.ToLowerInvariant()] = $true
    $src = Join-Path $here ("files\" + ($f.path -replace '/', '\'))
    if ((Get-Sha $src) -ne $f.sha256) { throw "mod file is damaged (hash mismatch): $($f.path). Re-download the mod." }
    $dst = In-Game $f.path
    $bak = In-Backup $f.path
    $cur = Get-ShaOrEmpty $dst
    $e = $state[$f.path]
    if ($f.path -ceq $DsrleIni) { $action = "ini" }
    elseif ($f.path.StartsWith("$DsrleDir/")) {
        # The folder is DSRLE's: no backups and no ownership checks, so hot-edited engine files never block a reinstall.
        $action = if ($cur -eq $f.sha256) { "current" } else { "data" }
    }
    elseif ($e -and $cur -eq $f.sha256) { $action = "current" }
    elseif ($cur -eq $f.sha256) {
        # DSRLE's file but no record of what it replaced (DSRLE_backup was deleted?). Never guess: the original would be lost.
        # d3d11.dll has no vanilla version, so there is no original to lose.
        if (-not $f.vanilla_sha256) { $action = "adopt" }
        elseif ((Test-Path -LiteralPath $bak) -and (Get-Sha $bak) -eq $f.vanilla_sha256) { $action = "adopt" }
        else { throw "$($f.path) is already the DSRLE version, but DSRLE_backup has no record of the original. Use Steam > Dark Souls Remastered > Properties > Installed Files > Verify integrity of game files, then run install.ps1 again." }
    }
    elseif ($e) {
        # An older DSRLE version, an interrupted install, or Steam put the vanilla file back: keep the first backup.
        # Anything else was written by someone else after DSRLE: do not overwrite it silently.
        $action = "update"
        if ($cur -and $e.installed_sha256 -and $e.status -ne "pending" -and $cur -ne $e.installed_sha256 -and $cur -ne $e.backup_sha256 -and $cur -ne $f.vanilla_sha256) {
            # A vanilla backup stays, as in 1.0: that mod can install its file again. Any other backup gives way to this
            # file, the one uninstall should bring back (d3d11.dll has no vanilla version, so this is always its case).
            $keepVanilla = $f.vanilla_sha256 -and $e.backup_sha256 -eq $f.vanilla_sha256
            if (-not $Force) {
                if ($f.path -ceq $DsrleDll) { throw "$dst was replaced by another file after DSRLE was installed (another mod?). Nothing was changed. Uninstall that mod first, or rerun with -Force: the file is backed up and uninstall.ps1 puts it back." }
                if ($keepVanilla) { throw "$($f.path) was replaced by something else after DSRLE was installed (another mod?). Nothing was changed. Uninstall that mod first, or rerun with -Force: the file is overwritten, and uninstall.ps1 puts back the vanilla file, not this one." }
                throw "$($f.path) was replaced by something else after DSRLE was installed (another mod?). Nothing was changed. Uninstall that mod first, or rerun with -Force: the file is backed up and uninstall.ps1 puts it back."
            }
            $action = if ($keepVanilla) { "overwrite" } else { "backup-foreign" }
        }
    }
    elseif ($cur -eq "") { $action = "add" }                  # file missing from the game folder: nothing to back up
    elseif ($f.vanilla_sha256 -and $cur -eq $f.vanilla_sha256) { $action = "backup" }
    elseif ($Force) { $action = "backup-foreign" }
    elseif ($f.path -ceq $DsrleDll) { throw "$dst is not this mod's file. It probably belongs to another mod (PvP Overhaul, OmniRoll standalone, ReShade...); only one d3d11.dll can exist. Nothing was changed. Uninstall that mod first, or rerun with -Force: the file is backed up and uninstall.ps1 puts it back, but that mod will not work while this one is installed." }
    else { throw "$($f.path) is neither vanilla nor DSRLE (another mod?). Uninstall that mod first, or rerun with -Force." }
    $plan += [pscustomobject]@{ f = $f; src = $src; dst = $dst; bak = $bak; cur = $cur; action = $action }
}
$plan = @($plan | Sort-Object { $_.f.path -ceq $DsrleDll })   # the DLL last: an interrupted install never leaves it without its data

# The engine swaps shaders by the checksum of the vanilla blob, so it needs the vanilla shader archives.
$restoreArchives = @()     # DSRLE 1.0's archives, to put back from 1.0's backup
$forgetArchives = @()      # record entries of archives that are vanilla again (Steam's Verify, for example)
$notVanilla = @()          # what stays not vanilla even after that
foreach ($r in $manifest.requires_vanilla) {
    if ($r.path -cnotmatch $DsrleAllow) { throw "manifest entry outside the allowlist: $($r.path)" }
    $seen[$r.path.ToLowerInvariant()] = $true     # so the older-version clean-up below leaves it to this check
    $dst = In-Game $r.path
    $bak = In-Backup $r.path
    $cur = Get-ShaOrEmpty $dst
    $e = $state[$r.path]
    if ($cur -eq $r.sha256) {
        if ($e -and -not $e.foreign) { $forgetArchives += $r.path }     # a foreign backup stays recorded: it is someone's file
        continue
    }
    $restorable = $e -and $e.backup_sha256 -and ($cur -eq "" -or $cur -eq $e.installed_sha256 -or $e.status -eq "pending") -and
        (Test-Path -LiteralPath $bak) -and (Get-Sha $bak) -eq $e.backup_sha256
    if ($restorable) { $restoreArchives += [pscustomobject]@{ path = $r.path; dst = $dst; bak = $bak; entry = $e; vanilla = ($e.backup_sha256 -eq $r.sha256) } }
    if (-not ($restorable -and $e.backup_sha256 -eq $r.sha256)) { $notVanilla += $r.path }
}
if ($notVanilla.Count -and -not $Force) {
    throw ("Not the vanilla file: " + ($notVanilla -join ", ") + ". The engine needs the vanilla shader archives, because its shader replacements are matched to them. Nothing was changed. Uninstall the mod that changed them, or use Steam > Dark Souls Remastered > Properties > Installed Files > Verify integrity of game files, then run install.ps1 again. Or rerun with -Force to install anyway: the shader replacements will not apply.")
}

# ---- Changes start here. ----
New-Item -ItemType Directory -Force $backup | Out-Null
Save-Record $record $manifest $state

foreach ($a in $restoreArchives) {
    $e = $a.entry
    $state[$a.path] = New-Entry $a.path $e.backup_sha256 ([bool]$e.foreign) $e.installed_sha256 "pending"
    Save-Record $record $manifest $state          # BEFORE the overwrite: an interrupted restore can be run again
    Copy-Item -LiteralPath $a.bak $a.dst -Force
    if ((Get-Sha $a.dst) -ne $e.backup_sha256) { throw "could not restore $($a.path)" }
    Remove-Item -LiteralPath $a.bak -Force
    $state.Remove($a.path)
    Save-Record $record $manifest $state
    if ($a.vanilla) { Write-Output "restored    $($a.path) to vanilla, from the DSRLE 1.0 backup (the engine does not use 1.0's archives)" }
    else { Write-Output "restored    $($a.path) from the DSRLE 1.0 backup (the file that was there before 1.0, not vanilla)" }
}
foreach ($k in $forgetArchives) {
    $bak = In-Backup $k
    if (Test-Path -LiteralPath $bak) { Remove-Item -LiteralPath $bak -Force }
    $state.Remove($k)
}

# Files an older DSRLE version installed that this version no longer ships: put the original back.
$dropped = @($state.Keys | Where-Object { -not $seen.ContainsKey($_.ToLowerInvariant()) })
foreach ($k in $dropped) {
    $e = $state[$k]
    if ($k -cnotmatch $DsrleAllow) { throw "unexpected entry in install.json: $k" }
    $dst = In-Game $k; $bak = In-Backup $k
    if ($k.StartsWith("$DsrleDir/")) {
        if (Test-Path -LiteralPath $dst) { Remove-Item -LiteralPath $dst -Force }
        $state.Remove($k)
        Write-Output "removed     $k (no longer part of DSRLE)"
        continue
    }
    $cur = Get-ShaOrEmpty $dst
    if ($cur -and $e.installed_sha256 -and $e.status -ne "pending" -and $cur -ne $e.installed_sha256 -and $cur -ne $e.backup_sha256) {
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
Save-Record $record $manifest $state

foreach ($p in $plan) {
    $f = $p.f
    New-Item -ItemType Directory -Force (Split-Path $p.dst) | Out-Null
    if ($p.action -eq "ini") {
        # The user's settings survive updates. The new defaults always land beside them.
        $default = In-Game $DsrleIniDefault
        Copy-Item -LiteralPath $p.src $default -Force
        if ((Get-Sha $default) -ne $f.sha256) { throw "copy failed for $DsrleIniDefault" }
        $state[$DsrleIniDefault] = New-Entry $DsrleIniDefault "" $false $f.sha256 "installed"
        $last = if ($state[$f.path]) { $state[$f.path].installed_sha256 } else { "" }
        if ($p.cur -and $p.cur -ne $f.sha256 -and $last -and $p.cur -eq $last) {
            # Still exactly what the version before wrote, so nothing of the user's is in it and it takes the new defaults.
            Copy-Item -LiteralPath $p.src $p.dst -Force
            if ((Get-Sha $p.dst) -ne $f.sha256) { throw "copy failed for $($f.path); run uninstall.ps1 to restore." }
            $state[$f.path] = New-Entry $f.path "" $false $f.sha256 "installed"
            Write-Output "updated     $($f.path) (it still held the last version's settings, unchanged)"
        } elseif ($p.cur) {
            # installed_sha256 stays what DSRLE last wrote to this file, or "" if it never did: a later version can tell an
            # edited file. A file that is this version's settings to the byte counts as written by it.
            $mine = if ($p.cur -eq $f.sha256) { $f.sha256 } else { $last }
            $state[$f.path] = New-Entry $f.path "" $false $mine "installed"
            if ($p.cur -ne $f.sha256) { Write-Output "kept        $($f.path) (your settings; the new defaults are in dsrle.ini.default, and a setting your file does not have takes its built-in default)" }
            else { Write-Output "up to date  $($f.path)" }
        } else {
            Copy-Item -LiteralPath $p.src $p.dst -Force
            if ((Get-Sha $p.dst) -ne $f.sha256) { throw "copy failed for $($f.path); run uninstall.ps1 to restore." }
            $state[$f.path] = New-Entry $f.path "" $false $f.sha256 "installed"
            Write-Output "installed   $($f.path)"
        }
        Save-Record $record $manifest $state
        continue
    }
    if ($p.action -eq "current") {
        $e = $state[$f.path]
        if ($f.path.StartsWith("$DsrleDir/")) { $state[$f.path] = New-Entry $f.path "" $false $f.sha256 "installed" }
        elseif ($e.status -ne "installed") { $state[$f.path] = New-Entry $f.path $e.backup_sha256 ([bool]$e.foreign) $f.sha256 "installed" }   # record from an older installer, or an install that was interrupted after this copy
        Write-Output "up to date  $($f.path)"; continue
    }
    if ($p.action -like "backup*" -or $p.action -eq "update") { New-Item -ItemType Directory -Force (Split-Path $p.bak) | Out-Null }
    switch -Wildcard ($p.action) {
        "backup*" {
            Copy-Item -LiteralPath $p.dst $p.bak -Force
            if ((Get-Sha $p.bak) -ne $p.cur) { throw "backup failed for $($f.path); nothing was changed for this file." }
            if ($p.action -eq "backup-foreign") {
                Write-Output "backed up   $($p.dst) (not this mod's file) to DSRLE_backup; uninstall.ps1 puts it back"
                if ($state[$f.path].backup_sha256) { Write-Warning "That backup takes the place of the earlier one of $($f.path): the file from before it will not come back." }
            }
            $state[$f.path] = New-Entry $f.path $p.cur ($p.action -eq "backup-foreign") $f.sha256 "pending"
        }
        "add"   { $state[$f.path] = New-Entry $f.path "" $false $f.sha256 "pending" }
        "data"  { $state[$f.path] = New-Entry $f.path "" $false $f.sha256 "pending" }
        "adopt" { $state[$f.path] = New-Entry $f.path $f.vanilla_sha256 $false $f.sha256 "pending" }
        "overwrite" {
            Write-Warning "Overwriting $($p.dst) (not this mod's file). It is not backed up: uninstall.ps1 puts back the vanilla file, not this one."
            $state[$f.path] = New-Entry $f.path $state[$f.path].backup_sha256 ([bool]$state[$f.path].foreign) $f.sha256 "pending"
        }
        "update" {
            $old = $state[$f.path]
            $bakSha = $old.backup_sha256
            $foreign = [bool]$old.foreign
            # The backup is gone or damaged, but the file here is an original (Steam's Verify put it back, say): it is the backup now.
            if ($bakSha -and $p.cur -and ($p.cur -eq $bakSha -or $p.cur -eq $f.vanilla_sha256) -and -not ((Test-Path -LiteralPath $p.bak) -and (Get-Sha $p.bak) -eq $bakSha)) {
                Copy-Item -LiteralPath $p.dst $p.bak -Force
                if ((Get-Sha $p.bak) -ne $p.cur) { throw "backup failed for $($f.path); nothing was changed for this file." }
                $bakSha = $p.cur
                if ($p.cur -eq $f.vanilla_sha256) { $foreign = $false }
            }
            $state[$f.path] = New-Entry $f.path $bakSha $foreign $f.sha256 "pending"
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
if ($notVanilla.Count) {
    Write-Warning ("Installed over shader archives that are not vanilla: " + ($notVanilla -join ", ") + ". The shader replacements will not apply for them.")
}
