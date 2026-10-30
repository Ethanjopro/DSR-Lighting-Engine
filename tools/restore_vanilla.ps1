# Restore every file listed in vanilla/manifest.sha256 into the game folder and verify hashes.
param([switch]$DrawParamOnly)
$root = Split-Path -Parent $PSScriptRoot
$game = "C:\Program Files (x86)\Steam\steamapps\common\DARK SOULS REMASTERED"
$bad = 0
foreach ($line in Get-Content "$root\vanilla\manifest.sha256") {
    $hash, $rel = $line -split '  ', 2
    if ($DrawParamOnly -and $rel -notlike 'param/DrawParam/*') { continue }
    $dst = Join-Path $game ($rel -replace '/', '\')
    $cur = if (Test-Path $dst) { (Get-FileHash $dst -Algorithm SHA256).Hash.ToLower() } else { "" }
    if ($cur -ne $hash) {
        Copy-Item (Join-Path "$root\vanilla" ($rel -replace '/', '\')) $dst -Force
        $cur = (Get-FileHash $dst -Algorithm SHA256).Hash.ToLower()
        Write-Output "restored $rel"
    }
    if ($cur -ne $hash) { $bad++; Write-Warning "hash mismatch after restore: $rel" }
}
if ($bad) { throw "$bad file(s) failed to restore" } else { Write-Output "all vanilla files verified" }
