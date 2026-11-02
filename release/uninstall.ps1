# DSR Lighting Engine uninstaller. Removes d3d11.dll and the dsrle folder, and puts back every file install.ps1 backed
# up, checking its hash. It works from the install record alone, so it also undoes an install by an earlier version.
#
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1 [-GameDir "D:\Steam\steamapps\common\DARK SOULS REMASTERED"] [-Force]
#
# A file that is no longer the one DSRLE installed (another mod replaced it afterwards) is left alone and reported.
# -Force: put the original back over it anyway. A d3d11.dll that is not DSRLE's is not deleted, even with -Force. The one
# exception: if the record shows an install was interrupted while writing d3d11.dll, whatever is there now cannot be told
# from that half-written copy, and is removed.
#
# If a backup is missing or damaged, use Steam > Dark Souls Remastered > Properties > Installed Files >
# Verify integrity of game files, then run this again. If the whole backup folder is missing, Verify restores the vanilla
# files, but Steam leaves files it does not know about, so also delete d3d11.dll (if it is this mod's) and the dsrle
# folder from the game folder by hand.
[CmdletBinding()]
param([string]$GameDir, [switch]$Force)
$ErrorActionPreference = "Stop"
trap { [Console]::Error.WriteLine("ERROR: " + $_.Exception.Message); exit 1 }
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

. (Join-Path $here "common.ps1")
$GameDir = Resolve-GameDir $GameDir

$backup = Join-Path $GameDir "DSRLE_backup"
$record = Join-Path $backup "install.json"
Restore-Record $record
if (-not (Test-Path -LiteralPath $record)) {
    throw "Nothing to uninstall: no DSRLE install record in $backup. If the mod is installed anyway, use Steam's 'Verify integrity of game files', and delete d3d11.dll (if it is this mod's) and the dsrle folder from the game folder by hand."
}
$files = @(Read-Record $record)

$bad = 0      # could not be restored yet: the backup folder stays, and a later run can finish
$lost = 0     # an original nothing can bring back: reported, and it does not hold up the rest
# The DLL first: an interruption never leaves it in place without its data.
foreach ($e in @($files | Sort-Object { $_.path -cne $DsrleDll })) {
    if ($e.path -cnotmatch $DsrleAllow) { Write-Warning "unexpected entry in install.json, skipped: $($e.path)"; $bad++; continue }
    if ($e.path.StartsWith("$DsrleDir/")) { continue }      # removed below with the folder
    $isDll = $e.path -ceq $DsrleDll
    $dst = Join-Path $GameDir ($e.path -replace '/', '\')
    $bak = Join-Path $backup ($e.path -replace '/', '\')
    $cur = Get-ShaOrEmpty $dst
    # Not DSRLE's file any more (and not a half-written one of ours): do not overwrite someone else's file.
    if ($cur -and $e.installed_sha256 -and $e.status -ne "pending" -and $cur -ne $e.installed_sha256 -and $cur -ne $e.backup_sha256) {
        if ($isDll -and -not $e.backup_sha256) {
            Write-Warning "$dst is not the file DSRLE installed (another mod?). Left alone."     # nothing to put back, and not DSRLE's to delete
            continue
        }
        if (-not $Force) {
            Write-Warning "$($e.path) was changed after DSRLE installed it (another mod?). Left alone; rerun with -Force to put the original back anyway."
            $bad++
            continue
        }
    }
    if (-not $e.backup_sha256) {
        if ($cur) { Remove-Item -LiteralPath $dst -Force }      # the mod added this file; there was nothing before
        Write-Output "removed     $($e.path)"
        continue
    }
    # Already the original (an earlier run that stopped before its clean-up, or Steam's Verify): no backup needed.
    if ($cur -eq $e.backup_sha256) { Write-Output "restored    $($e.path)"; continue }
    if (-not (Test-Path -LiteralPath $bak) -or (Get-Sha $bak) -ne $e.backup_sha256) {
        $ours = $cur -and ($cur -eq $e.installed_sha256 -or $e.status -eq "pending")
        if ($isDll) {
            # The other mod's DLL cannot come back, but ours still goes: it must not stay without its data.
            if ($ours) { Remove-Item -LiteralPath $dst -Force; Write-Output "removed     $($e.path)" }
            Write-Warning "backup missing or damaged: $($e.path). The other mod's d3d11.dll has to be reinstalled from that mod."
            $lost++
        }
        elseif ($Force -and $cur -and -not $ours) {
            Write-Warning "backup missing or damaged: $($e.path). The file there now is not DSRLE's, so it stays as it is. Use Steam's 'Verify integrity of game files' if you want the vanilla one."
            $lost++
        }
        else {
            Write-Warning "backup missing or damaged: $($e.path). Use Steam's 'Verify integrity of game files', then run uninstall again."
            $bad++
        }
        continue
    }
    Copy-Item -LiteralPath $bak $dst -Force
    if ((Get-Sha $dst) -ne $e.backup_sha256) { Write-Warning "restore failed: $($e.path)"; $bad++; continue }
    Write-Output "restored    $($e.path)"
}

# The whole folder, whatever the DLL wrote there: log, traces, dumps, trigger files. The user's settings go with it.
$dataDir = Join-Path $GameDir $DsrleDir
if (Test-Path -LiteralPath $dataDir -PathType Container) {
    Remove-DirTree $dataDir
    Write-Output "removed     $DsrleDir\ (settings, logs and traces included)"
}

if ($bad) { throw "$bad file(s) could not be restored; the backup folder was kept." }
# Only an install by 1.0 can hold these: it set another mod's file aside this way when -Force overwrote it.
foreach ($c in @(Get-ChildItem -LiteralPath $backup -Recurse -Force -File | Where-Object { $_.Name -like "*.replaced-*" })) {
    Write-Warning "$($c.FullName) is a copy of another mod's file, set aside by an earlier install -Force. It goes with the backup folder; reinstall that mod if you still want it."
}
Remove-DirTree $backup "install.json"      # the record last: while it exists, uninstall can run again
if ($lost) { Write-Output "DSR Lighting Engine removed, but $lost original file(s) could not be put back: see the warnings above." }
else { Write-Output "DSR Lighting Engine removed; the original files are back." }
