@echo off
setlocal
cd /d "%~dp0"

echo === 创建虚拟环境 .venv ===
python -m venv .venv
if errorlevel 1 (
    echo [错误] 创建失败，请确认已安装 Python 3.11 并加入 PATH。
    pause
    exit /b 1
)

echo === 升级 pip 并按 requirements.lock 安装依赖 ===
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -i https://mirrors.aliyun.com/pypi/simple --trusted-host mirrors.aliyun.com -r requirements.lock
if errorlevel 1 (
    echo [错误] 依赖安装失败，请重试或检查网络。
    pause
    exit /b 1
)

echo === 完成：依赖已安装。下一步：复制 .env.example 为 .env 并填写密钥，然后双击 start.bat ===
pause