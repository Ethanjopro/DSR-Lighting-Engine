# Wrap an offline test session in the save swap. Before: the game must be closed and the live save must be the
# recorded real save; vanilla files are restored and the test save goes in. After (also on failure): the game is
# closed, vanilla files are restored, the test save is kept in saves\test_session_end.sl2, and the real save is
# put back and hash-verified.
#
#   .\tools\test_session.ps1 -Script { & .\tools\shader_session.ps1 -Run x -Variants vanilla -Only firelink }
param([Parameter(Mandatory)] [scriptblock]$Script)
$root = Split-Path -Parent $PSScriptRoot
Import-Module "$root\tools\dsr.psm1" -Force
Import-Module "$root\tools\gadget.psm1" -Force

$live = Join-Path $env:USERPROFILE "Documents\NBGI\DARK SOULS REMASTERED\<Steam account folder>\DRAKS0005.sl2"
$test = Join-Path $root "saves\test_session_end.sl2"
$ledger = Get-Content -Raw (Join-Path $root "saves\ledger.json") | ConvertFrom-Json
$realSha = $ledger.original_save.sha256
$realBak = Join-Path $root $ledger.original_save.backed_up

if (Test-DsrRunning) { throw "the game is running: close it first" }
if ((Get-FileSha $live) -ne $realSha) { throw "the live save is not the recorded real save: refusing to swap" }
if ((Get-FileSha $realBak) -ne $realSha) { throw "the real-save backup does not match the ledger" }
if (-not (Test-Path $test)) { throw "no test save at $test" }
& "$root\tools\restore_vanilla.ps1" | Out-Null
Copy-Item $test (Join-Path $root "saves\backup\test_session_end.before-$(Get-Date -Format yyyyMMdd-HHmmss).sl2")
Copy-Item $test $live -Force
Write-Output "test save in (previous copy kept in saves\backup); running session"
$sessionStart = Get-Date

try {
    & $Script
} finally {
    Stop-Gadget
    if (Test-DsrRunning) {
        try {
            if ((Get-DsrState) -eq "InGame") { Exit-DsrToTitle }
            Close-DsrAtTitle
        } catch {
            Write-Warning "clean exit failed ($_); closing the game"
            Stop-DsrHard
        }
    }
    for ($i = 0; $i -lt 30 -and (Test-DsrRunning); $i++) { Start-Sleep 1 }
    if (Test-DsrRunning) { Write-Warning "the game did not exit; killing it"; Stop-DsrHard; Start-Sleep 3 }
    # A crash can leave a Windows Error Reporting process holding the game's files.
    Get-CimInstance Win32_Process -Filter "Name='WerFault.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CreationDate -ge $sessionStart } |
        ForEach-Object { Write-Warning "closing WerFault (pid $($_.ProcessId)) left by a crash"; Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    # A failed file restore must never skip the save restore below; it is reported after the save is back.
    $restoreError = $null
    try { & "$root\tools\restore_vanilla.ps1" | Out-Null } catch { $restoreError = $_ }
    Copy-Item $live $test -Force
    Copy-Item $realBak $live -Force
    $sha = Get-FileSha $live
    if ($sha -ne $realSha) { Write-Error "REAL SAVE RESTORE FAILED: live save hash $sha" }
    else { Write-Output "real save restored and verified ($($sha.Substring(0, 8)))" }
    if ($restoreError) { Write-Error "VANILLA FILE RESTORE FAILED: $restoreError" }
}
