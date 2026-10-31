# DSR Lighting Engine uninstaller. Puts back every file install.ps1 backed up and checks its hash.
#
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1 [-GameDir "D:\Steam\steamapps\common\DARK SOULS REMASTERED"] [-Force]
#
# A file that is no longer the one DSRLE installed (another mod replaced it afterwards) is left alone and reported.
# -Force: put the original back over it anyway.
#
# If the backup folder is missing, use Steam > Dark Souls Remastered > Properties > Installed Files >
# Verify integrity of game files instead; it restores the vanilla files.
[CmdletBinding()]
param([string]$GameDir, [switch]$Force)
$ErrorActionPreference = "Stop"
trap { [Console]::Error.WriteLine("ERROR: " + $_.Exception.Message); exit 1 }
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

. (Join-Path $here "common.ps1")
$GameDir = Resolve-GameDir $GameDir

$backup = Join-Path $GameDir "DSRLE_backup"
$record = Join-Path $backup "install.json"
if (-not (Test-Path -LiteralPath $record)) { throw "Nothing to uninstall: no DSRLE install record in $backup. If the mod is installed anyway, use Steam's 'Verify integrity of game files'." }
$files = @((Get-Content -Raw -LiteralPath $record | ConvertFrom-Json).files)

$bad = 0
foreach ($e in $files) {
    if ($e.path -cnotmatch $DsrleAllow) { Write-Warning "unexpected entry in install.json, skipped: $($e.path)"; $bad++; continue }
    $dst = Join-Path $GameDir ($e.path -replace '/', '\')
    $bak = Join-Path $backup ($e.path -replace '/', '\')
    $cur = if (Test-Path -LiteralPath $dst) { Get-Sha $dst } else { "" }
    # Not DSRLE's file any more (and not a half-written one of ours): do not overwrite someone else's file.
    if ($e.installed_sha256 -and $e.status -ne "pending" -and $cur -ne $e.installed_sha256 -and $cur -ne $e.backup_sha256 -and -not $Force) {
        Write-Warning "$($e.path) was changed after DSRLE installed it (another mod?). Left alone; rerun with -Force to put the original back anyway."
        $bad++
        continue
    }
    if (-not $e.backup_sha256) {
        if (Test-Path -LiteralPath $dst) { Remove-Item -LiteralPath $dst -Force }      # the mod added this file; there was nothing before
        Write-Output "removed     $($e.path)"
        continue
    }
    if (-not (Test-Path -LiteralPath $bak) -or (Get-Sha $bak) -ne $e.backup_sha256) {
        Write-Warning "backup missing or damaged: $($e.path). Use Steam's 'Verify integrity of game files'."
        $bad++
        continue
    }
    if ($cur -ne $e.backup_sha256) { Copy-Item -LiteralPath $bak $dst -Force }
    if ((Get-Sha $dst) -ne $e.backup_sha256) { Write-Warning "restore failed: $($e.path)"; $bad++; continue }
    Write-Output "restored    $($e.path)"
}
if ($bad) { throw "$bad file(s) could not be restored; the backup folder was kept." }
Remove-Item -LiteralPath $backup -Recurse -Force
Write-Output "DSR Lighting Engine removed; the original files are back."
