# -*- coding: utf-8 -*-
"""
Gunicorn 生产配置
==================
启动：gunicorn -c gunicorn.conf.py wsgi:application

要点：
  - 用 gthread 工作模式而非 gevent：本项目以 IO 阻塞型（抓取行情/新闻）为主，
    线程池即可获得并发收益，且兼容 pandas/akshare 这类 C 扩展。
  - worker 数不必大：数据抓取受上游限流约束，2 个 worker × 8 线程足够。
  - timeout 调大：选股接口要逐只拉取行情，可能耗时 60s+。
"""
import multiprocessing
import os

# 端口：优先用平台注入的 PORT（Render/Railway/Heroku 都这么干），
# 否则回落到 8000（本地/Docker 用）
_port = os.getenv("PORT", "8000")
bind = os.getenv("BIND", "0.0.0.0:%s" % _port)

# 数据抓取是 IO 密集 + 上游限流，worker 保持克制，靠线程提并发。
# 免费平台上内存通常只有 512MB，1 个 worker 更稳（pandas+akshare 吃内存），
# 用 WEB_CONCURRENCY 环境变量可覆盖。
workers = int(os.getenv("WEB_CONCURRENCY", 1))
worker_class = "gthread"
threads = int(os.getenv("WEB_THREADS", 8))

# 选股/批量分析耗时较长，给足超时
timeout = 180
graceful_timeout = 30
keepalive = 5

# 日志
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("LOG_LEVEL", "info")

# 前置代理（Nginx/云平台 LB）场景下正确识别真实客户端
forwarded_allow_ips = "*"
proxy_allow_ips = "*"

# 进程名，便于 ps 查看
proc_name = "stocklens"

# 抢占式重启，防止长时间运行内存膨胀
max_requests = 1000
max_requests_jitter = 100

def on_starting(server):
    server.log.info("StockLens 启动中 —— bind=%s workers=%s threads=%s",
                    bind, workers, threads)
