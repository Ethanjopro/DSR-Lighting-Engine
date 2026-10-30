# DSR Lighting Engine uninstaller. Puts back every file install.ps1 backed up and checks its hash.
#
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1 [-GameDir "D:\Steam\steamapps\common\DARK SOULS REMASTERED"]
#
# If the backup folder is missing, use Steam > Dark Souls Remastered > Properties > Installed Files >
# Verify integrity of game files instead; it restores the vanilla files.
param([string]$GameDir)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

. (Join-Path $here "common.ps1")
$GameDir = Resolve-GameDir $GameDir

$backup = Join-Path $GameDir "DSRLE_backup"
$record = Join-Path $backup "install.json"
if (-not (Test-Path $record)) { throw "No DSRLE install record in $backup. Use Steam's 'Verify integrity of game files' instead." }
$files = (Get-Content -Raw $record | ConvertFrom-Json).files

$bad = 0
foreach ($e in $files) {
    $dst = Join-Path $GameDir ($e.path -replace '/', '\')
    $bak = Join-Path $backup ($e.path -replace '/', '\')
    if (-not $e.backup_sha256) {
        if (Test-Path $dst) { Remove-Item $dst -Force }      # the mod added this file; there was nothing before
        Write-Output "removed     $($e.path)"
        continue
    }
    if (-not (Test-Path $bak) -or (Get-Sha $bak) -ne $e.backup_sha256) {
        Write-Warning "backup missing or damaged: $($e.path). Use Steam's 'Verify integrity of game files'."
        $bad++
        continue
    }
    Copy-Item $bak $dst -Force
    if ((Get-Sha $dst) -ne $e.backup_sha256) { Write-Warning "restore failed: $($e.path)"; $bad++; continue }
    Write-Output "restored    $($e.path)"
}
if ($bad) { throw "$bad file(s) could not be restored; the backup folder was kept." }
Remove-Item $backup -Recurse -Force
Write-Output "DSR Lighting Engine removed; the original files are back."
