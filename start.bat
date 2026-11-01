@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 没找到 python。请先安装 Python 3.10+，安装时勾选 "Add Python to PATH"。
  pause
  exit /b 1
)

if not exist ".env" (
  if exist ".env.example" (
    copy /y ".env.example" ".env" >nul
    echo [提示] 已生成 .env 模板。可以在网页「设置 → 模型 API」里填，也可以直接编辑 .env。
  )
)

python -c "import fastapi, uvicorn, httpx, yaml, dashscope" >nul 2>nul
if errorlevel 1 (
  echo [1/2] 正在安装依赖...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络或手动运行：python -m pip install -r requirements.txt
    pause
    exit /b 1
  )
)

echo [2/2] 启动服务：http://127.0.0.1:8077
echo       手机用同一 Wi-Fi 打开 http://^<这台电脑的局域网IP^>:8077
echo       配对码会打印在下面，也写在 配对码.txt 里。
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:8077'"
python -u server.py --port 8077

pause
