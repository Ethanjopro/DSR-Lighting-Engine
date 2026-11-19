# Build the engine DLL. Output: engine\bin\d3d11.dll (x64, static CRT, no third-party libraries).
#   .\engine\build.ps1 [-Debug]
param([switch]$Debug)
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$src = Join-Path $here "src"
$obj = Join-Path $here "obj"
$bin = Join-Path $here "bin"
New-Item -ItemType Directory -Force $obj, $bin | Out-Null

$vcvars = & "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -find "VC\Auxiliary\Build\vcvars64.bat"
if (-not $vcvars) { throw "MSVC x64 tools not found" }

# The shipped settings, built in as the defaults a dsrle.ini is read over. Without the [dev] section: a build's own
# switches are not a default.
$dev = $false
$lines = foreach ($line in Get-Content (Join-Path $here "data\dsrle.ini")) {
    if ($line -match '^\s*\[') { $dev = $line -match '^\s*\[dev\]' }
    if (-not $dev) { '"' + $line.Replace('\', '\\').Replace('"', '\"') + '\n"' }
}
Set-Content -Encoding ascii (Join-Path $obj "default_ini.inc") $lines

$opt = if ($Debug) { "/Od /Zi" } else { "/O2 /Zi /GL" }
$ltcg = if ($Debug) { "" } else { "/LTCG /OPT:REF /OPT:ICF" }
$sources = (Get-ChildItem $src -Filter *.cpp | ForEach-Object { '"' + $_.FullName + '"' }) -join " "

# Shell state does not persist between tool calls, so vcvars and the compile share one cmd line.
$script = @"
@echo off
set "PATH=%PATH%;%ProgramFiles(x86)%\Microsoft Visual Studio\Installer"
call "$vcvars" >nul 2>&1 || exit /b 1
cd /d "$obj" || exit /b 1
ml64 /nologo /c /Fo exports.obj "$src\exports.asm" || exit /b 1
cl /nologo /c /std:c++20 /EHsc /MT /W4 /wd4100 /wd4201 $opt /DUNICODE /D_UNICODE /DNOMINMAX /DDSRLE_DEFAULT_INI /I"$src" /I"$obj" $sources || exit /b 1
link /nologo /DLL /DEBUG $ltcg /DEF:"$src\exports.def" /OUT:"$bin\d3d11.dll" /PDB:"$bin\d3d11.pdb" /MAP:"$bin\d3d11.map" *.obj dxguid.lib user32.lib || exit /b 1
"@
$bat = Join-Path $obj "build.cmd"
Set-Content -Encoding ascii $bat $script
cmd /c "`"$bat`""
if ($LASTEXITCODE -ne 0) { throw "engine build failed ($LASTEXITCODE)" }
Write-Output ("built {0} ({1:N0} bytes)" -f (Join-Path $bin "d3d11.dll"), (Get-Item (Join-Path $bin "d3d11.dll")).Length)
