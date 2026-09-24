@echo off
setlocal
cd /d "%~dp0"
title Arc SmartMoney - Analyse et Paper
if not exist ".venv\Scripts\python.exe" (
  echo Python du projet introuvable : .venv\Scripts\python.exe
  pause
  exit /b 1
)
".venv\Scripts\python.exe" run_all.py
echo.
echo Lanceur termine. Les donnees sont conservees.
pause
