@echo off
setlocal
cd /d "%~dp0"
title Arc SmartMoney - Etat
".venv\Scripts\python.exe" run_all.py --status
echo.
".venv\Scripts\python.exe" status.py
echo.
".venv\Scripts\python.exe" -m paper_copy --status
pause
