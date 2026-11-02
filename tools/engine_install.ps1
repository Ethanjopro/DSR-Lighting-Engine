# Install or remove the engine DLL for a test session. Removal keeps the session's log and traces by moving
# the game's dsrle folder under captures\<KeepAs>. Only touches d3d11.dll and the dsrle folder in the game dir.
#   .\tools\engine_install.ps1                     # install engine\bin\d3d11.dll and engine\data
#   .\tools\engine_install.ps1 -Remove -KeepAs x   # remove, keep logs in captures\x\engine
# Test sessions run in SDR unless asked, so their shots stay comparable with earlier runs. The swap chain is made when
# the game starts, so -Hdr has to be chosen here, not by a settings variant.
param([switch]$Remove, [string]$KeepAs, [string]$DrawParam, [switch]$Hdr)
$root = Split-Path -Parent $PSScriptRoot
$game = "C:\Program Files (x86)\Steam\steamapps\common\DARK SOULS REMASTERED"
$dll = Join-Path $game "d3d11.dll"
$dir = Join-Path $game "dsrle"

if ($Remove) {
    if (Get-Process -Name DarkSoulsRemastered -ErrorAction SilentlyContinue) { throw "the game is running" }
    if (Test-Path $dll) { Remove-Item $dll -Force }
    if (Test-Path $dir) {
        if ($KeepAs) {
            $keep = Join-Path $root "captures\$KeepAs\engine"
            New-Item -ItemType Directory -Force (Split-Path $keep) | Out-Null
            if (Test-Path $keep) { Remove-Item $keep -Recurse -Force }
            Move-Item $dir $keep
        } else {
            Remove-Item $dir -Recurse -Force
        }
    }
    Write-Output "engine removed"
    return
}

$src = Join-Path $root "engine\bin\d3d11.dll"
if (-not (Test-Path $src)) { throw "engine not built: run engine\build.ps1" }
Copy-Item $src $dll -Force
New-Item -ItemType Directory -Force $dir | Out-Null
Copy-Item (Join-Path $root "engine\data\*") $dir -Recurse -Force
if (-not $Hdr) { Add-Content -Path (Join-Path $dir "dsrle.ini") -Value "`n[hdr]`nenabled = 0" }
Write-Output ("engine installed ({0:N0} bytes)" -f (Get-Item $dll).Length)
# Optional: the 1.0 per-area DrawParam build. restore_vanilla.ps1 puts the vanilla files back after the session.
if ($DrawParam) {
    $src = Join-Path $root $DrawParam
    foreach ($f in Get-ChildItem (Join-Path $src "param\DrawParam") -File -Filter *.parambnd.dcx) {
        Copy-Item $f.FullName (Join-Path $game "param\DrawParam\$($f.Name)") -Force
    }
    Write-Output "DrawParam installed from $DrawParam"
}
