@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [提示] 未检测到 .venv，首次运行请先双击 install.bat 安装依赖。
    pause
    exit /b 1
)

if not exist ".env" (
    echo [提示] 未检测到 .env，请复制 .env.example 为 .env 并填写密钥后重试。
    pause
    exit /b 1
)

echo 启动 enterprise-copilot 一体化网关：http://127.0.0.1:8000
echo 按 Ctrl+C 停止服务。
.venv\Scripts\python.exe -m uvicorn gateway.app:app --host 127.0.0.1 --port 8000