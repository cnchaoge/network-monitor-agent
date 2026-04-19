@echo off
chcp 65001 >nul
echo ====================================
echo  网络监控 Agent Windows 打包工具
echo  版本: v0.4.0
echo ====================================
echo.

REM 安装依赖
echo [1/3] 检查依赖...
pip install pystray pillow pyinstaller -q
if errorlevel 1 (
    echo 依赖安装失败，请以管理员身份运行
    pause
    exit /b 1
)

REM 清理旧文件
echo.
echo [2/3] 清理旧构建...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

REM 打包
echo.
echo [3/3] 打包中（首次较慢，约1-2分钟）...
python -m pyinstaller --onefile --noconsole --name "NetworkMonitorAgent_v0400" --distpath . windows_agent.py

if errorlevel 1 (
    echo.
    echo 打包失败！
    pause
    exit /b 1
)

echo.
echo ====================================
echo  打包完成！
echo  exe 文件: NetworkMonitorAgent_v0400.exe
echo ====================================
echo.
echo 运行方法：双击 NetworkMonitorAgent_v0400.exe
echo 开机自启：首次运行后勾选「开机自动启动」
echo.
pause
