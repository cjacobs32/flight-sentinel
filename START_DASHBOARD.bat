@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" main.py --dashboard
) else (
  py -3 main.py --dashboard
)
pause
