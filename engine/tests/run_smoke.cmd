@echo off
rem Build wrap_smoke against the import library, then run it with the proxy DLL beside the exe.
set "PATH=%PATH%;%ProgramFiles(x86)%\Microsoft Visual Studio\Installer"
for /f "usebackq delims=" %%i in (`"%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe" -latest -products * -find VC\Auxiliary\Build\vcvars64.bat`) do call "%%i" >nul 2>&1
if not exist "%~dp0..\obj\smoke" mkdir "%~dp0..\obj\smoke"
cd /d "%~dp0..\obj\smoke"
cl /nologo /EHsc /std:c++20 /O1 "%~dp0wrap_smoke.cpp" d3d11.lib dxgi.lib dxguid.lib user32.lib /Fewrap_smoke.exe || exit /b 1
copy /y "%~dp0..\bin\d3d11.dll" . >nul
if exist dsrle rmdir /s /q dsrle
.\wrap_smoke.exe
type dsrle\dsrle.log
