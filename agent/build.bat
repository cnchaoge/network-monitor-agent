@echo off
echo ====================================
echo  网络监控 Agent Windows 打包工具
echo ====================================
echo.

REM 安装依赖
echo [1/2] 安装依赖...
pip install pystray pillow pyinstaller -q
if errorlevel 1 (
    echo 依赖安装失败，请以管理员身份运行
    pause
    exit /b 1
)

REM 打包
echo.
echo [2/2] 打包中（首次较慢，约1-2分钟）...
pyinstaller --onefile --noconsole --name "NetworkMonitorAgent" --distpath . windows_agent.py

if errorlevel 1 (
    echo.
    echo 打包失败！
    pause
    exit /b 1
)

echo.
echo ====================================
echo  打包完成！
echo  exe 文件: NetworkMonitorAgent.exe
echo ====================================
echo.
echo 运行方法：双击 NetworkMonitorAgent.exe
echo 开机自启：把 NetworkMonitorAgent.exe 复制到以下目录：
echo   %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\
echo.
pause
