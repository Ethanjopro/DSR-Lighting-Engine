@echo off
rem Build maps_test with the engine's maps, config and common sources, then run it beside a fresh dsrle folder.
rem   run_maps_test.cmd                      the cases on the synthetic table
rem   run_maps_test.cmd <table> [<dir>]      read a real table (absolute paths), and check the DrawParam in dir
set "PATH=%PATH%;%ProgramFiles(x86)%\Microsoft Visual Studio\Installer"
for /f "usebackq delims=" %%i in (`"%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe" -latest -products * -find VC\Auxiliary\Build\vcvars64.bat`) do call "%%i" >nul 2>&1
if not exist "%~dp0..\obj\maps_test" mkdir "%~dp0..\obj\maps_test"
cd /d "%~dp0..\obj\maps_test"
cl /nologo /EHsc /std:c++20 /MT /W4 /wd4100 /wd4201 /O1 /DUNICODE /D_UNICODE /DNOMINMAX "%~dp0maps_test.cpp" "%~dp0..\src\maps.cpp" "%~dp0..\src\config.cpp" "%~dp0..\src\common.cpp" user32.lib /Femaps_test.exe || exit /b 1
if exist dsrle rmdir /s /q dsrle
if exist param rmdir /s /q param
.\maps_test.exe %*
