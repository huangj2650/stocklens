# StockLens · 生产镜像
# 构建：docker build -t stocklens .
# 运行：docker run -d -p 8000:8000 --name stocklens stocklens
FROM python:3.11-slim

# 时区设为东八区，保证行情/新闻时间正确
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 系统依赖：lxml 编译所需（akshare 解析 HTML 用）
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ libxml2-dev libxslt1-dev tzdata curl \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖，利用 Docker 层缓存
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# 再拷贝代码
COPY . .

# 缓存目录（可挂载卷持久化）
RUN mkdir -p /app/data/cache

EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

# 生产启动：gunicorn
CMD ["gunicorn", "-c", "gunicorn.conf.py", "wsgi:application"]
