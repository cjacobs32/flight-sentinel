@echo off
setlocal
cd /d "%~dp0"

echo ==============================================
echo   Flight Sentinel V2 - Setup Windows
echo ==============================================
echo.

set "PY_CMD="
set "PY_ARGS="

rem Preferir Python Launcher de Windows. -3 elige el Python 3 moderno instalado
rem (incluye 3.14, 3.13, 3.12, etc.) y evita los shims de pyenv-win del PATH.
where py >nul 2>&1
if not errorlevel 1 (
    py -3 -c "import sys; assert sys.version_info >= (3,10)" >nul 2>&1
    if not errorlevel 1 (
        set "PY_CMD=py"
        set "PY_ARGS=-3"
        goto :python_found
    )
)

rem Fallback: python del PATH, solo si ejecuta Python >= 3.10.
python -c "import sys; assert sys.version_info >= (3,10)" >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=python"
    set "PY_ARGS="
    goto :python_found
)

echo [ERROR] No encontre una instalacion utilizable de Python 3.10 o superior.
echo.
echo Ejecuta este comando para ver tus versiones instaladas:
echo     py -0p
echo.
echo NO uses Python 3.7.4: Flight Sentinel V2 requiere Python 3.10 o superior.
goto :error_pause

:python_found
echo Python seleccionado:
%PY_CMD% %PY_ARGS% --version
if errorlevel 1 goto :error_pause

echo.
if exist ".venv\Scripts\python.exe" (
    echo Entorno .venv existente. Se reutilizara.
) else (
    echo Creando entorno virtual .venv...
    %PY_CMD% %PY_ARGS% -m venv .venv
    if errorlevel 1 goto :error_pause
)

echo Actualizando pip...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :error_pause

echo Instalando dependencias...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :error_pause

if not exist monitors.yaml copy monitors.yaml.example monitors.yaml >nul
if not exist .env copy .env.example .env >nul

echo.
echo ==============================================
echo   INSTALACION COMPLETADA
echo ==============================================
echo.
echo Siguiente paso:
echo   1. Completa .env con SERPAPI_API_KEY.
echo   2. Ejecuta START_DASHBOARD.bat
echo.
pause
exit /b 0

:error_pause
echo.
echo No se pudo completar la instalacion.
pause
exit /b 1
