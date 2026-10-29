@echo off
rem Start backend (new window) then frontend dev server
start "PICKEMUP API" cmd /c "%~dp0start-api.bat"
cd /d "%~dp0..\apps\web"
if not exist node_modules call npm install
call npm run dev
