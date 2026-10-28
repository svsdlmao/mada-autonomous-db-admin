@echo off
REM Starts MADA with Docker Desktop, then open http://localhost:8000
cd /d "%~dp0"
docker compose up --build
pause
