@echo off
echo ====================================
echo  Network Monitor Agent - Build Tool
echo ====================================
echo.

REM Install dependencies
echo [1/3] Installing dependencies...
pip install pystray pillow pyinstaller -q
if errorlevel 1 (
    echo [ERROR] Failed to install dependencies. Try running as Administrator.
    pause
    exit /b 1
)

REM Clean old build
echo.
echo [2/3] Cleaning old build files...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

REM Build
echo.
echo [3/3] Building exe (first run may take 1-2 minutes)...
python -m pyinstaller --onefile --noconsole --name "NetworkMonitorAgent" --distpath . windows_agent.py

if errorlevel 1 (
    echo.
    echo [ERROR] Build failed!
    pause
    exit /b 1
)

echo.
echo ====================================
echo  Build complete!
echo  Output: dist\NetworkMonitorAgent.exe
echo ====================================
echo.
echo Run: double-click NetworkMonitorAgent.exe
echo Auto-start: enable via Settings in system tray
echo.
pause
