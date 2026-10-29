@echo off
rem ============================================================================
rem  PICKEMUP PORTABLE - runs entirely from this folder (USB drive, external
rem  SSD, any Windows PC). Nothing is installed on the host machine:
rem    * Python venv, node_modules, npm cache, logs and the SQLite database
rem      all live inside this folder.
rem    * No registry writes, no AppData, no admin rights.
rem  Host requirements (checked below, never installed silently):
rem    * Python 3.11+  (or the 'uv' launcher)
rem    * Node.js 18+   (only for the dashboard; API-only mode works without)
rem  Ports: set PICKEMUP_API_PORT / PICKEMUP_WEB_PORT before launch to override.
rem ============================================================================
setlocal EnableExtensions
cd /d "%~dp0"
set "ROOT=%~dp0"
set "API_PORT=%PICKEMUP_API_PORT%"
if "%API_PORT%"=="" set "API_PORT=8090"
set "WEB_PORT=%PICKEMUP_WEB_PORT%"
if "%WEB_PORT%"=="" set "WEB_PORT=3001"
rem keep every tool cache inside the portable folder, never on the host
set "npm_config_cache=%ROOT%.npmcache"
set "PIP_CACHE_DIR=%ROOT%.pipcache"
set "UV_CACHE_DIR=%ROOT%.uvcache"
set "DATABASE_URL=sqlite:///%ROOT%data\pickemup.db"
title PICKEMUP Portable (api:%API_PORT% web:%WEB_PORT%)

echo(
echo  PICKEMUP PORTABLE - everything stays on this drive
echo  ---------------------------------------------------
if not exist "%ROOT%data" mkdir "%ROOT%data"

rem ---------- python ----------
set "PY=%ROOT%.venv\Scripts\python.exe"
if exist "%PY%" goto :py_ok
echo [1/5] first run on this machine - building the local venv...
where uv >nul 2>&1
if %errorlevel%==0 (
  uv venv "%ROOT%.venv" 1>nul 2>nul
  uv pip install --python "%PY%" -e . 2>nul
  if exist "%PY%" goto :py_ok
)
where python >nul 2>&1 || goto :no_python
python -m venv "%ROOT%.venv" || goto :no_python
"%PY%" -m pip install --quiet -e .
if not exist "%PY%" goto :no_python
:py_ok
echo       python venv ready

rem ---------- .env ----------
if not exist "%ROOT%.env" copy "%ROOT%.env.example" "%ROOT%.env" >nul
echo [2/5] configuration: %ROOT%.env

rem ---------- API ----------
echo [3/5] starting API + worker on 127.0.0.1:%API_PORT% ...
start "PICKEMUP API (portable)" /min cmd /c "cd /d "%ROOT%" && "%PY%" -m uvicorn pickemup_api.main:app --host 127.0.0.1 --port %API_PORT%"

rem ---------- health ----------
echo       waiting for the API...
set "HEALTHY="
for /l %%i in (1,1,40) do (
  if not defined HEALTHY (
    "%PY%" -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%API_PORT%/health',timeout=2).status==200 else 1)" >nul 2>&1 && set "HEALTHY=1"
    if not defined HEALTHY timeout /t 1 /nobreak >nul
  )
)
if not defined HEALTHY (
  echo  [!] API did not answer on port %API_PORT% - close other PICKEMUP windows
  echo      or set PICKEMUP_API_PORT to a free port, then re-run this file.
  pause
  exit /b 1
)
echo       API healthy

rem ---------- dashboard ----------
where node >nul 2>&1
if errorlevel 1 goto :api_only
echo [4/5] dashboard on 127.0.0.1:%WEB_PORT% ...
if not exist "%ROOT%apps\web\node_modules" (
  echo       installing frontend dependencies on this drive - first run only, may take a few minutes
  pushd "%ROOT%apps\web"
  call npm install --no-fund --no-audit
  popd
)
start "PICKEMUP Web (portable)" /min cmd /c "cd /d "%ROOT%apps\web" && set "NEXT_PUBLIC_API_URL=http://127.0.0.1:%API_PORT%" && call npx next dev -p %WEB_PORT%"
:open
start "" "http://127.0.0.1:%WEB_PORT%"
goto :done

:api_only
echo [4/5] Node.js not found on this PC - starting in API-only mode.
echo       The full console needs Node 18+; the API and all jobs still work:
echo       http://127.0.0.1:%API_PORT%/docs
start "" "http://127.0.0.1:%API_PORT%/docs"

:done
echo [5/5] PICKEMUP is running. Keep this window open.
echo       close the two small black windows -or- press any key here to stop everything.
pause >nul
taskkill /fi "WINDOWTITLE eq PICKEMUP API*" /f >nul 2>&1
taskkill /fi "WINDOWTITLE eq PICKEMUP Web*" /f >nul 2>&1
exit /b 0

:no_python
echo  [!] No usable Python found. Install Python 3.11+ (python.org) once on the
echo      host, or put the free 'uv' launcher on PATH - then re-run this file.
echo      Everything PICKEMUP needs afterwards lives on THIS drive.
pause
exit /b 1
