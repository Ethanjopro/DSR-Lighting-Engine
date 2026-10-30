# Shared helpers for install.ps1 / uninstall.ps1 (dot-sourced; defines functions only).

function Get-Sha([string]$Path) { (Get-FileHash $Path -Algorithm SHA256).Hash.ToLower() }

# The Steam library folder that holds Dark Souls Remastered, or $null.
function Find-GameDir {
    $roots = @()
    try { $roots += (Get-ItemProperty "HKCU:\Software\Valve\Steam" -ErrorAction Stop).SteamPath } catch {}
    $roots += "C:\Program Files (x86)\Steam"
    foreach ($r in $roots) {
        $vdf = Join-Path $r "steamapps\libraryfolders.vdf"
        $libs = @($r)
        if (Test-Path $vdf) {
            $libs += (Select-String -Path $vdf -Pattern '"path"\s+"([^"]+)"' | ForEach-Object { $_.Matches[0].Groups[1].Value -replace '\\\\', '\' })
        }
        foreach ($l in $libs) {
            $d = Join-Path $l "steamapps\common\DARK SOULS REMASTERED"
            if (Test-Path (Join-Path $d "DarkSoulsRemastered.exe")) { return $d }
        }
    }
    return $null
}

function Resolve-GameDir([string]$GameDir) {
    if (-not $GameDir) { $GameDir = Find-GameDir }
    if (-not $GameDir -or -not (Test-Path (Join-Path $GameDir "DarkSoulsRemastered.exe"))) {
        throw "Dark Souls Remastered not found. Pass -GameDir `"<path to DARK SOULS REMASTERED>`"."
    }
    if (Get-Process -Name DarkSoulsRemastered -ErrorAction SilentlyContinue) { throw "Close Dark Souls Remastered first." }
    return $GameDir
}
