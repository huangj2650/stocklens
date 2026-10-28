@echo off
chcp 65001 >nul
title StockLens 股票分析体系
cd /d "%~dp0"

echo ================================================
echo    StockLens 股票分析体系 - 一键启动
echo ================================================
echo.

REM ---------- 1. 检查 Python ----------
where python >nul 2>nul
if errorlevel 1 (
    where py >nul 2>nul
    if errorlevel 1 (
        echo [错误] 没有检测到 Python
        echo.
        echo 请先安装 Python：https://www.python.org/downloads/
        echo 安装时务必勾选 "Add Python to PATH"
        echo.
        pause
        exit /b 1
    )
    set PY=py
) else (
    set PY=python
)

echo [1/4] 检测到 Python：
%PY% --version
echo.

REM ---------- 2. 创建虚拟环境 ----------
if not exist ".venv\Scripts\python.exe" (
    echo [2/4] 首次运行，正在创建独立环境（约需10秒）...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败
        pause
        exit /b 1
    )
) else (
    echo [2/4] 环境已存在
)
set VPY=.venv\Scripts\python.exe
echo.

REM ---------- 3. 安装依赖 ----------
if not exist ".venv\.installed" (
    echo [3/4] 正在安装依赖，首次约需 2-5 分钟，请耐心等待...
    %VPY% -m pip install --upgrade pip -q
    %VPY% -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    if errorlevel 1 (
        echo [错误] 依赖安装失败，请检查网络后重试
        pause
        exit /b 1
    )
    echo ok > .venv\.installed
    echo 依赖安装完成
) else (
    echo [3/4] 依赖已就绪
)
echo.

REM ---------- 4. 启动 ----------
echo [4/4] 正在启动服务...
echo.
echo ================================================
echo    服务地址：http://127.0.0.1:8000
echo    浏览器会自动打开
echo.
echo    ★ 关闭此窗口即可停止服务
echo ================================================
echo.

start "" http://127.0.0.1:8000
%VPY% app.py --port 8000

pause
