@echo off
setlocal
cd /d "%~dp0"
echo ==============================================
echo   Flight Sentinel V2.3 - Actualizacion
 echo ==============================================
echo.
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] No existe .venv. Ejecuta SETUP_WINDOWS.bat primero.
  pause
  exit /b 1
)
echo Instalando/actualizando dependencias de V2.3...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :error
echo.
echo Verificando Playwright...
".venv\Scripts\python.exe" -c "from playwright.sync_api import sync_playwright; print('Playwright OK')"
if errorlevel 1 goto :error
echo.
echo No se descarga Chromium: V2.3 usa el Google Chrome instalado en Windows.
echo Tu .env, monitors.yaml y flight_monitor.db no se modifican.
echo.
echo ACTUALIZACION COMPLETADA.
echo Inicia con START_DASHBOARD.bat
pause
exit /b 0
:error
echo.
echo [ERROR] No se pudo completar la actualizacion.
pause
exit /b 1
