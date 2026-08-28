@echo off
rem Start the A-end HTTP proxy (Windows VM, embeddable Python C:\Python311).
setlocal
cd /d "%~dp0"

echo [A] Starting Clipboard Git Tunnel (A proxy, listen 127.0.0.1:9998)...
echo [A] Extra args passed through: %*
C:\Python311\python.exe a_end\a_proxy.py ^
    --listen 127.0.0.1:9998 ^
    --chunk-bytes 262144 ^
    --ack-timeout 5 ^
    --retries 5 ^
    --timeout 300 ^
    %*

echo.
echo [A] Tunnel exited with errorlevel %errorlevel%.
pause
