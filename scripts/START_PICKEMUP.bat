@echo off
rem ============================================================
rem  PICKEMUP one-click starter - double-click to run everything
rem  Bootstraps on first use, then starts API + dashboard + browser
rem ============================================================
setlocal
cd /d "%~dp0.."

echo [1/5] Checking Python virtualenv...
if not exist .venv\Scripts\python.exe (
  echo     First run: creating venv and installing dependencies - this takes a few minutes.
  where uv >nul 2>nul
  if %errorlevel%==0 (
    uv venv .venv --python 3.14 || goto :pyfail
    set VIRTUAL_ENV=%CD%\.venv
    uv pip install -e ".[dev]" || goto :pyfail
  ) else (
    python -m venv .venv || goto :pyfail
    .venv\Scripts\pip install -e ".[dev]" || goto :pyfail
  )
)
if not exist .env copy .env.example .env >nul

echo [2/5] Checking Node.js...
where node >nul 2>nul || (
  echo     Node.js not found. Install it from https://nodejs.org ^(LTS^) and rerun this launcher.
  pause & exit /b 1
)
pushd apps\web
if not exist node_modules (
  echo     Installing dashboard dependencies - first run only...
  call npm install || (popd & exit /b 1)
)
popd

echo [3/5] Starting API + worker ^(port 8090^)...
start "PICKEMUP API" cmd /c ""%~dp0start-api.bat""

echo [4/5] Waiting for the API...
set TRIES=0
:waitapi
powershell -NoProfile -Command "try { exit (1 - [int]((Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8090/health -TimeoutSec 2).StatusCode -eq 200)) } catch { exit 1 }" && goto :apiready
set /a TRIES+=1
if %TRIES% geq 30 (echo     API did not become healthy - see the "PICKEMUP API" window. & pause & exit /b 1)
timeout /t 2 /nobreak >nul
goto :waitapi
:apiready

echo [5/5] Starting dashboard ^(port 3000^) and opening the browser...
start "PICKEMUP Web" cmd /c "cd /d "%~dp0..\apps\web" && npm run dev"
timeout /t 6 /nobreak >nul
start "" http://localhost:3000
echo.
echo   PICKEMUP is running. Close the two server windows to stop it.
echo   Data lives in .\data\pickemup.db - it is never sent anywhere.
timeout /t 8 >nul
exit /b 0

:pyfail
echo     Python setup failed. Install Python 3.11+ (or uv) and rerun.
pause
exit /b 1
