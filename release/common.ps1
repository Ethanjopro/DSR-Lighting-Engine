# Shared helpers for install.ps1 / uninstall.ps1 (dot-sourced; defines functions and the allowlist).
# Every path goes through -LiteralPath: game folders such as "D:\Games [SSD]\..." must not be read as wildcards.

# The only files DSRLE may ever write, restore or remove by name (case-sensitive, anchored; no trailing newline
# accepted). shader/ stays listed although 2.x ships no archive: a 1.0 install record names them and must stay undoable.
# dsrle/ paths cannot leave that folder: every segment starts with a letter, digit or underscore, so no "..".
# tools/package.py reads this line and refuses to ship anything it does not match.
$DsrleAllow = '\A(param/DrawParam/a1[0-8]_DrawParam\.parambnd\.dcx|shader/FRPG_(Filter|FlverPBL_fpo)_DX11\.shaderbnd\.dcx|d3d11\.dll|dsrle/(hlsl/)?[A-Za-z0-9_][A-Za-z0-9_.-]*)\z'

$DsrleDll = "d3d11.dll"
$DsrleDir = "dsrle"
$DsrleIni = "dsrle/dsrle.ini"
$DsrleIniDefault = "dsrle/dsrle.ini.default"

function Get-Sha([string]$Path) { (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLower() }

# The file's hash, or "" when it is not there.
function Get-ShaOrEmpty([string]$Path) { if (Test-Path -LiteralPath $Path -PathType Leaf) { Get-Sha $Path } else { "" } }

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
    if ($GameDir) {
        $GameDir = $GameDir.Trim().Trim('"').TrimEnd('\', '/')
        # With more arguments after it, that quote swallows them: a -Force after the path never arrives.
        if ($GameDir.Contains('"')) {
            throw "The backslash before the closing quote of -GameDir hides whatever follows it. Nothing was changed. Remove that backslash, or put -Force before -GameDir."
        }
    }
    if (-not $GameDir) { $GameDir = Find-GameDir }
    if (-not $GameDir -or -not (Test-Path -LiteralPath (Join-Path $GameDir "DarkSoulsRemastered.exe"))) {
        throw "Dark Souls Remastered not found. Pass -GameDir `"<path to DARK SOULS REMASTERED>`"."
    }
    if (Get-Process -Name DarkSoulsRemastered -ErrorAction SilentlyContinue) { throw "Close Dark Souls Remastered first." }
    return (Resolve-Path -LiteralPath $GameDir).ProviderPath      # absolute: the .NET calls below ignore PowerShell's current folder
}

# Deletes a folder and what is in it, and nothing else. A junction or symlink is removed as a link: its target is
# outside the folder and stays. (Remove-Item -Recurse does not promise that on Windows PowerShell 5.1.)
# $Last names a file to delete after the rest of this folder.
function Remove-DirTree([string]$Path, [string]$Last) {
    $item = Get-Item -LiteralPath $Path -Force
    if (-not ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        foreach ($c in @(Get-ChildItem -LiteralPath $Path -Force | Sort-Object { $_.Name -ieq $Last })) {
            if ($c.PSIsContainer) { Remove-DirTree $c.FullName }
            else { Remove-Item -LiteralPath $c.FullName -Force }
        }
    }
    if ($item.Attributes -band [IO.FileAttributes]::ReadOnly) { $item.Attributes = $item.Attributes -band (-bnot [IO.FileAttributes]::ReadOnly) }
    [IO.Directory]::Delete($Path)
}

# A parsed install record, or $null if the file is empty or damaged.
function Get-Record([string]$Path) {
    $text = Get-Content -Raw -LiteralPath $Path      # outside the try: a locked file is not a damaged one
    $rec = $null
    try { $rec = $text | ConvertFrom-Json } catch {}
    if ($null -eq $rec -or $null -eq $rec.files) { return $null }
    foreach ($e in @($rec.files)) { if ($e.path -isnot [string]) { return $null } }
    return $rec
}

# Run this before looking for the record. An interruption in Save-Record's last step leaves no record under its own name,
# and the finished new one under the .tmp name (any version's Save-Record): put it back. A .tmp that does not parse is the
# first save of a fresh install cut short: nothing had been changed yet, so there is no record to bring back.
function Restore-Record([string]$Path) {
    $tmp = $Path + ".tmp"
    if ((Test-Path -LiteralPath $Path) -or -not (Test-Path -LiteralPath $tmp)) { return }
    if ($null -ne (Get-Record $tmp)) { Move-Item -LiteralPath $tmp -Destination $Path }
}

# The entries of an install record. A record that cannot be read stops the script: read as "no entries", it would look
# like nothing to restore, and the backups it describes would be thrown away.
function Read-Record([string]$Path) {
    $rec = Get-Record $Path
    if ($null -eq $rec) {
        throw "The install record $Path is empty or damaged. Nothing was changed. To remove the mod by hand, use Steam's 'Verify integrity of game files', then delete d3d11.dll (if it is this mod's), the dsrle folder and DSRLE_backup from the game folder. If DSRLE_backup holds a d3d11.dll, it is another mod's file that DSRLE replaced: copy it back to the game folder first."
    }
    $rec.files
}

# One install record entry: what a file was before DSRLE, and what DSRLE put there.
#   status "pending"   = DSRLE is about to write / may have half-written the file (restore it unconditionally)
#   status "installed" = DSRLE's file is complete (a different hash later means someone else changed it)
# dsrle/ entries are informational: that folder is DSRLE's, and uninstall removes it whole.
function New-Entry([string]$Path, [string]$BackupSha, [bool]$Foreign, [string]$InstalledSha, [string]$Status) {
    [pscustomobject]@{ path = $Path; backup_sha256 = $BackupSha; foreign = $Foreign; installed_sha256 = $InstalledSha; status = $Status }
}

# Written BEFORE a file is overwritten (and after), so an interrupted install can always be uninstalled. $Path must be
# absolute (.NET calls). The new record is complete on disk before it takes the old one's name. That last step has a moment
# with no record under the name (Move-Item -Force deletes the old file first; File.Replace moves it aside first, so it is
# no better), which is what Restore-Record is for.
function Save-Record([string]$Path, $Manifest, $State) {
    $tmp = $Path + ".tmp"
    $json = [pscustomobject]@{ mod = $Manifest.name; version = $Manifest.version; files = @($State.Values | Sort-Object path) } | ConvertTo-Json -Depth 4
    $bytes = [Text.Encoding]::UTF8.GetBytes($json)
    if ([IO.File]::Exists($tmp)) { [IO.File]::SetAttributes($tmp, [IO.FileAttributes]::Normal) }     # left by an interrupted run
    $fs = [IO.File]::Open($tmp, [IO.FileMode]::Create, [IO.FileAccess]::Write)
    try { $fs.Write($bytes, 0, $bytes.Length); $fs.Flush($true) } finally { $fs.Close() }
    Move-Item -LiteralPath $tmp -Destination $Path -Force
}
