@echo off
rem Start the B-end Git forwarder (cloud desktop).
rem Reads python path and B-side options from config.yaml; extra args pass through.
setlocal
cd /d "%~dp0"

set "PY_EXE=python"
if exist "%~dp0config.yaml" (
    for /f "usebackq delims=" %%L in (`findstr /b /c:"python:" "%~dp0config.yaml"`) do set "CFG_PY_LINE=%%L"
    if defined CFG_PY_LINE (
        set "CFG_PY=%CFG_PY_LINE:*python:=%"
        if defined CFG_PY set "CFG_PY=%CFG_PY: =%"
        if defined CFG_PY set "CFG_PY=%CFG_PY:"=%"
        if defined CFG_PY set "PY_EXE=%CFG_PY%"
    )
)

echo [B] Using python: %PY_EXE%
echo [B] Starting Clipboard Git Tunnel (B forwarder, config=%~dp0config.yaml)...
echo [B] Extra args passed through: %*
"%PY_EXE%" b_end\b_tunnel.py --config "%~dp0config.yaml" %*

echo.
echo [B] Tunnel exited with errorlevel %errorlevel%.
pause