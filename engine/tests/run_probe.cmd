@echo off
set "PATH=%PATH%;%ProgramFiles(x86)%\Microsoft Visual Studio\Installer"
for /f "usebackq delims=" %%i in (`"%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe" -latest -products * -find VC\Auxiliary\Build\vcvars64.bat`) do call "%%i" >nul 2>&1
if not exist "%~dp0..\obj\tests" mkdir "%~dp0..\obj\tests"
cd /d "%~dp0..\obj\tests"
cl /nologo /EHsc /std:c++20 /O1 "%~dp0%1.cpp" d3d11.lib dxgi.lib dxguid.lib user32.lib /Fe%1.exe || exit /b 1
.\%1.exe

