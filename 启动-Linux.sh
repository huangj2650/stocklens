#!/bin/bash
# StockLens 股票分析体系 - Linux 一键启动
cd "$(dirname "$0")"

echo "================================================"
echo "   StockLens 股票分析体系 - 一键启动"
echo "================================================"
echo ""

# ---------- 1. 检查 Python ----------
PY=""
for c in python3 python; do
    if command -v $c &> /dev/null; then
        PY=$c
        break
    fi
done

if [ -z "$PY" ]; then
    echo "[错误] 没有检测到 Python3"
    echo ""
    echo "Ubuntu/Debian 请执行：sudo apt install -y python3 python3-venv python3-pip"
    echo "CentOS/RHEL  请执行：sudo yum install -y python3 python3-pip"
    echo ""
    read -p "按回车键退出..."
    exit 1
fi

echo "[1/4] 检测到：$($PY --version)"
echo ""

# ---------- 2. 创建虚拟环境 ----------
if [ ! -f ".venv/bin/python" ]; then
    echo "[2/4] 首次运行，正在创建独立环境..."
    $PY -m venv .venv
    if [ $? -ne 0 ]; then
        echo "[错误] 创建虚拟环境失败"
        echo "若提示缺少 venv，请先执行：sudo apt install -y python3-venv"
        read -p "按回车键退出..."
        exit 1
    fi
else
    echo "[2/4] 环境已存在"
fi
echo ""

# ---------- 3. 安装依赖 ----------
if [ ! -f ".venv/.installed" ]; then
    echo "[3/4] 正在安装依赖，首次约需 2-5 分钟..."
    .venv/bin/python -m pip install --upgrade pip -q
    .venv/bin/python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    if [ $? -ne 0 ]; then
        echo "[错误] 依赖安装失败，请检查网络后重试"
        read -p "按回车键退出..."
        exit 1
    fi
    touch .venv/.installed
    echo "依赖安装完成"
else
    echo "[3/4] 依赖已就绪"
fi
echo ""

# ---------- 4. 启动 ----------
echo "[4/4] 正在启动服务..."
echo ""
echo "================================================"
echo "   服务地址：http://127.0.0.1:8000"
echo "   手动在浏览器打开上面地址即可"
echo ""
echo "   ★ 按 Ctrl+C 即可停止服务"
echo "================================================"
echo ""

.venv/bin/python app.py --port 8000
