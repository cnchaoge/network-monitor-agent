#!/bin/bash
# lanwatch_agent Linux 打包脚本
# 用法: bash build_linux.sh

set -e

echo "===================================="
echo " lanwatch Agent Linux 打包工具"
echo "===================================="
echo

# 安装依赖
echo "[1/3] 安装依赖..."
pip install pystray pillow pyinstaller -q

# 清理旧构建
echo
echo "[2/3] 清理旧构建..."
rm -rf build dist

# 打包
echo
echo "[3/3] 打包中（首次较慢，约1-2分钟）..."
pyinstaller --onefile --name "lanwatch_agent" lanwatch_agent_linux.py

echo
echo "===================================="
echo " 打包完成！"
echo " 输出: dist/lanwatch_agent"
echo "===================================="
echo
echo "运行: ./dist/lanwatch_agent"
echo "自启: cp lanwatch_agent.desktop ~/.config/autostart/  (已自动配置)"
