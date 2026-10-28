# -*- coding: utf-8 -*-
"""
WSGI 入口 —— 生产环境使用
===========================
给 gunicorn / uWSGI / 云平台（Render、Railway、Fly.io、阿里云等）调用。

    gunicorn -c gunicorn.conf.py wsgi:application

云平台通常要求有个名为 application 或 app 的对象，这里都暴露。
"""
from app import app as application

# 兼容常见平台约定
app = application

if __name__ == "__main__":
    # 允许直接 python3 wsgi.py 启动（用于快速自测）
    application.run(host="0.0.0.0", port=8000)
