# Shared helpers for install.ps1 / uninstall.ps1 (dot-sourced; defines functions and the allowlist).
# Every path goes through -LiteralPath: game folders such as "D:\Games [SSD]\..." must not be read as wildcards.

# The only files DSRLE may ever write or restore (case-sensitive, anchored; no trailing newline accepted).
$DsrleAllow = '\A(param/DrawParam/a1[0-8]_DrawParam\.parambnd\.dcx|shader/FRPG_(Filter|FlverPBL_fpo)_DX11\.shaderbnd\.dcx)\z'

function Get-Sha([string]$Path) { (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLower() }

# The Steam library folder that holds Dark Souls Remastered, or $null.
function Find-GameDir {
    $roots = @()
    try { $sp = (Get-ItemProperty "HKCU:\Software\Valve\Steam" -ErrorAction Stop).SteamPath; if ($sp) { $roots += $sp } } catch {}
    $roots += "C:\Program Files (x86)\Steam"
    foreach ($r in $roots) {
        $vdf = Join-Path $r "steamapps\libraryfolders.vdf"
        $libs = @($r)
        if (Test-Path -LiteralPath $vdf) {
            $libs += (Select-String -LiteralPath $vdf -Pattern '"path"\s+"([^"]+)"' | ForEach-Object { $_.Matches[0].Groups[1].Value -replace '\\\\', '\' })
        }
        foreach ($l in $libs) {
            $d = Join-Path $l "steamapps\common\DARK SOULS REMASTERED"
            if (Test-Path -LiteralPath (Join-Path $d "DarkSoulsRemastered.exe")) { return $d }
        }
    }
    return $null
}

function Resolve-GameDir([string]$GameDir) {
    # -GameDir "X:\...\DARK SOULS REMASTERED\" (tab completion adds the last backslash): Windows turns \" into a
    # literal quote, so the argument arrives as  X:\...\REMASTERED"  - strip it.
    if ($GameDir) { $GameDir = $GameDir.Trim().Trim('"').TrimEnd('\', '/') }
    if (-not $GameDir) { $GameDir = Find-GameDir }
    if (-not $GameDir -or -not (Test-Path -LiteralPath (Join-Path $GameDir "DarkSoulsRemastered.exe"))) {
        throw "Dark Souls Remastered not found. Pass -GameDir `"<path to DARK SOULS REMASTERED>`"."
    }
    if (Get-Process -Name DarkSoulsRemastered -ErrorAction SilentlyContinue) { throw "Close Dark Souls Remastered first." }
    return $GameDir
}

# One install record entry: what a file was before DSRLE, and what DSRLE put there.
#   status "pending"   = DSRLE is about to write / may have half-written the file (restore it unconditionally)
#   status "installed" = DSRLE's file is complete (a different hash later means someone else changed it)
function New-Entry([string]$Path, [string]$BackupSha, [bool]$Foreign, [string]$InstalledSha, [string]$Status) {
    [pscustomobject]@{ path = $Path; backup_sha256 = $BackupSha; foreign = $Foreign; installed_sha256 = $InstalledSha; status = $Status }
}

# Written BEFORE a file is overwritten (and after), so an interrupted install can always be uninstalled.
function Save-Record([string]$Path, $Manifest, $State) {
    [pscustomobject]@{ mod = $Manifest.name; version = $Manifest.version; files = @($State.Values | Sort-Object path) } |
        ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 -LiteralPath $Path
}
