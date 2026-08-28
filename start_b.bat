@echo off
rem Start the B-end Git forwarder (cloud desktop, installed Python 3.11).
setlocal
cd /d "%~dp0"

echo [B] Starting Clipboard Git Tunnel (B forwarder, target 192.168.21.14:8888)...
echo [B] Extra args passed through: %*
python b_end\b_tunnel.py ^
    --target 192.168.21.14:8888 ^
    --chunk-bytes 262144 ^
    --ack-timeout 5 ^
    --retries 5 ^
    --timeout 300 ^
    %*

echo.
echo [B] Tunnel exited with errorlevel %errorlevel%.
pause
