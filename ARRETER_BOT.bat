@echo off
setlocal
cd /d "%~dp0"
title Arc SmartMoney - Arret
".venv\Scripts\python.exe" run_all.py --stop
pause
