@echo off
rem Builds PICKEMUP-Setup-0.1.0.exe:
rem   1) stages a clean source tree (no runtimes, no caches, no data, no git)
rem   2) compiles packaging\pickemup.iss with Inno Setup 6
rem Inno Setup itself is fetched via winget on demand (dev-machine action).
setlocal
cd /d "%~dp0.."
set "ROOT=%CD%"
set "STAGE=%ROOT%\packaging\stage"
set "OUT=%ROOT%\dist"

echo [1/4] staging tracked source tree (git archive - never ships untracked junk)...
if exist "%STAGE%" rmdir /s /q "%STAGE%"
mkdir "%STAGE%"
git -C "%ROOT%" archive --format=zip -o "%STAGE%.zip" HEAD
if not exist "%STAGE%.zip" (echo [!] git archive failed & pause & exit /b 1)
tar -xf "%STAGE%.zip" -C "%STAGE%"
if errorlevel 1 (echo [!] extraction failed & pause & exit /b 1)
del "%STAGE%.zip"

echo [2/4] locating Inno Setup 6 (iscc.exe)...
set "ISCC="
for %%P in (
  "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
  "%ProgramFiles%\Inno Setup 6\ISCC.exe"
  "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
) do if exist %%P set "ISCC=%%~P"
if not "%ISCC%"=="" goto :compile
echo     not found - installing via winget (dev tool)...
winget install -e --id JRSoftware.InnoSetup --accept-source-agreements --accept-package-agreements --silent
for %%P in (
  "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
  "%ProgramFiles%\Inno Setup 6\ISCC.exe"
  "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
) do if exist %%P set "ISCC=%%~P"
if "%ISCC%"=="" (
  echo  [!] Inno Setup unavailable. Install it manually:
  echo      winget install -e --id JRSoftware.InnoSetup
  echo      then re-run this script. The .iss is ready in packaging\pickemup.iss
  pause
  exit /b 1
)

:compile
echo [3/4] compiling installer...
if not exist "%OUT%" mkdir "%OUT%"
"%ISCC%" "%ROOT%\packaging\pickemup.iss"
if errorlevel 1 (echo [!] ISCC failed & pause & exit /b 1)

echo [4/4] done:
dir /b "%OUT%\PICKEMUP-Setup-*.exe"
pause
