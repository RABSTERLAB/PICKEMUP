@echo off
cd /d "%~dp0.."
if not exist .env copy .env.example .env >nul
.venv\Scripts\python.exe -m uvicorn pickemup_api.main:app --host 127.0.0.1 --port 8090
