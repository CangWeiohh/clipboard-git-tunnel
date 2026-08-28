@echo off
rem Start the A-end HTTP proxy (Windows VM).
rem Reads python path and A-side options from config.yaml; extra args pass through.
setlocal
cd /d "%~dp0"

set "PY_EXE=python"
if exist "%~dp0config.yaml" (
    for /f "usebackq delims=" %%L in (`findstr /b /c:"a_python:" "%~dp0config.yaml"`) do set "CFG_PY_LINE=%%L"
    if defined CFG_PY_LINE (
        set "CFG_PY=%CFG_PY_LINE:*a_python:=%"
        if defined CFG_PY set "CFG_PY=%CFG_PY: =%"
        if defined CFG_PY set "CFG_PY=%CFG_PY:"=%"
        if defined CFG_PY set "PY_EXE=%CFG_PY%"
    )
)

echo [A] Using python: %PY_EXE%
echo [A] Starting Clipboard Git Tunnel (A proxy, config=%~dp0config.yaml)...
echo [A] Extra args passed through: %*
"%PY_EXE%" a_end\a_proxy.py --config "%~dp0config.yaml" %*

echo.
echo [A] Tunnel exited with errorlevel %errorlevel%.
pause