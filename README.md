# 企业网络监控平台

## 项目结构

```
network-monitor/
├── server/                  # 云服务器端
│   ├── main.py
│   └── requirements.txt
├── agent/                   # 客户端 Agent
│   ├── agent.py            # 通用版（Linux/Mac）
│   ├── windows_agent.py     # Windows 版（托盘 + 开机自启）
│   ├── build.bat           # Windows 打包脚本
│   ├── requirements.txt    # pip 依赖
│   └── README.md           # 本文件
└── README.md               # 项目说明
```

## 服务端部署

已在腾讯云部署：
- 地址: http://82.156.229.67:8000
- 管理后台: http://82.156.229.67:8000
- 手机端: http://82.156.229.67:8000/mobile

## Windows Agent 部署步骤

1. 把 `agent/` 目录里的 `windows_agent.py` 复制到客户 Windows 电脑
2. 安装 Python（python.com 下载，安装时勾选 Add to PATH）
3. 安装依赖：`pip install pystray pillow pyinstaller`
4. 运行打包脚本：`build.bat`
5. 双击生成的 `NetworkMonitorAgent.exe` 即可运行

### 开机自启

把 `NetworkMonitorAgent.exe` 复制到：
```
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\
```

## 路由器部署

- 如果是 OpenWrt 路由器，可以装 Python 包
- 否则建议在内网找一台常年开机的 Windows PC 部署 Agent

## 手机端

直接访问 `http://82.156.229.67:8000/mobile?id=AGENT_ID`

## API

- `POST /api/register` - 注册 Agent
- `POST /api/{id}/report` - 上报数据
- `GET /api/{id}/latest` - 最新数据
- `GET /api/{id}/history` - 历史记录
- `GET /api/agents` - 所有设备列表
- `GET /health` - 健康检查
