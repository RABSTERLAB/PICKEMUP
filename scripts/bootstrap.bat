@echo off
rem PICKEMUP one-time setup: virtualenv + deps (requires uv or pip)
cd /d "%~dp0.."
where uv >nul 2>nul && (uv venv .venv --python 3.14 && set VIRTUAL_ENV=%CD%\.venv&& uv pip install -e ".[dev]") || (python -m venv .venv && .venv\Scripts\pip install -e ".[dev]")
echo Setup complete. Next: copy .env.example to .env, then run start-api.bat
pause
