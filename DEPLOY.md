# StockLens 部署指南

把本系统部署成一个**公网可访问的在线网站**，需要一台有公网 IP 的服务器。本文给出三条路径，按省心程度排序。

> **核心原理**：本项目是一个标准的 Python Web 应用（Flask + gunicorn）。
> 只要把它跑在一台能上公网的机器上，任何人就能通过 `http://你的域名或IP:端口` 访问。

---

## 路径一：Docker 部署（推荐，通用）

适合：任意云服务器（阿里云 / 腾讯云 / 华为云 / AWS / 轻量应用服务器）。

### 1. 准备服务器
- 购买一台云服务器（**2核2G 起步**，推荐 2核4G；系统选 Ubuntu 22.04）
- 在云控制台**安全组/防火墙**里放行你要用的端口（如 `8000`，或 80/443）

### 2. 上传代码
```bash
# 方式A：本地打包上传
tar czf stocklens.tar.gz --exclude='__pycache__' --exclude='data/cache' stock-analyzer
scp stocklens.tar.gz root@你的服务器IP:/opt/
ssh root@你的服务器IP
cd /opt && tar xzf stocklens.tar.gz && cd stock-analyzer
```

### 3. 构建并启动
```bash
docker build -t stocklens .
docker run -d \
  --name stocklens \
  --restart always \
  -p 8000:8000 \
  -v /opt/stocklens-cache:/app/data/cache \
  stocklens
```

### 4. 验证
```bash
docker ps                    # 应看到 stocklens 状态 Up
curl http://127.0.0.1:8000/api/health
```
然后浏览器打开 `http://你的服务器IP:8000` 即可。

---

## 路径二：直接部署（无需 Docker）

适合：已有 Python 环境的服务器。

```bash
cd /opt/stock-analyzer

# 1. 建虚拟环境（推荐，避免污染系统 Python）
python3 -m venv .venv
source .venv/bin/activate

# 2. 装依赖
pip install --upgrade pip
pip install -r requirements.txt

# 3. 启动（前台试跑，确认无误）
gunicorn -c gunicorn.conf.py wsgi:application
```

确认能访问后，用 **systemd** 做成开机自启的后台服务：

```bash
sudo tee /etc/systemd/system/stocklens.service > /dev/null <<'EOF'
[Unit]
Description=StockLens 股票分析体系
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/stock-analyzer
Environment="BIND=0.0.0.0:8000"
ExecStart=/opt/stock-analyzer/.venv/bin/gunicorn -c gunicorn.conf.py wsgi:application
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now stocklens
sudo systemctl status stocklens
```

---

## 路径三：托管平台（最省心，免运维）

适合：不想碰服务器，希望 `git push` 就上线。

这类平台（Render / Railway / Fly.io / 阿里云函数计算等）**直接读 `Dockerfile` 或自动识别 Python 项目**：

1. 把项目推到 GitHub 仓库
2. 在平台新建 Web Service，关联该仓库
3. 构建方式选 **Dockerfile**（本仓库已内置），或手动指定：
   - 构建命令：`pip install -r requirements.txt`
   - 启动命令：`gunicorn -c gunicorn.conf.py wsgi:application`
4. 平台会自动分配一个公网域名（如 `https://stocklens-xxx.onrender.com`）

> 注意：部分平台默认给的实例内存较小（512MB），pandas + akshare 可能吃紧，
> 建议选 **1GB 以上内存**规格。

---

## 绑定域名 + HTTPS（进阶）

想让地址变成 `https://yourdomain.com`：

### 1. 域名解析
在域名服务商处添加 A 记录，指向服务器公网 IP。

### 2. Nginx 反向代理
```bash
sudo apt install -y nginx
sudo tee /etc/nginx/sites-available/stocklens > /dev/null <<'EOF'
server {
    listen 80;
    server_name yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 180s;   # 选股接口耗时较长，需放宽
        proxy_connect_timeout 30s;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/stocklens /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

### 3. 申请免费 SSL 证书
```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d yourdomain.com
```
certbot 会自动改好 Nginx 配置并配置自动续期。

---

## 配置项说明

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `BIND` | `0.0.0.0:8000` | 监听地址与端口 |
| `WEB_CONCURRENCY` | `2` | gunicorn worker 进程数 |
| `WEB_THREADS` | `8` | 每个 worker 的线程数 |
| `LOG_LEVEL` | `info` | 日志级别 |

调整示例：
```bash
WEB_CONCURRENCY=4 WEB_THREADS=12 BIND=0.0.0.0:80 gunicorn -c gunicorn.conf.py wsgi:application
```

---

## 常见问题

**Q：页面能打开但数据加载很慢？**
首次请求需要抓取上游数据（新浪/东财/Nasdaq），之后走缓存会快很多。
磁盘缓存位于 `data/cache/`，**建议用卷挂载持久化**（Docker 命令里的 `-v` 就是干这个）。

**Q：选股功能超时？**
选股要逐只拉取行情，耗时 20–90 秒。已把 gunicorn `timeout` 设为 180s、
Nginx `proxy_read_timeout` 设为 180s。若仍超时，可调小候选池规模。

**Q：能部署在国内服务器吗？**
可以。项目用的数据源（新浪财经、东方财富、Nasdaq）在国内网络下访问正常。
但若要抓美股，注意 Nasdaq API 的连通性。

**Q：需要数据库吗？**
当前不需要。自选股存内存，缓存存磁盘。若要持久化用户与自选股，
可加 SQLite（零配置）或 PostgreSQL。

**Q：怎么更新代码？**
```bash
# Docker 方式
docker build -t stocklens . && docker stop stocklens && docker rm stocklens
docker run -d --name stocklens --restart always -p 8000:8000 stocklens

# systemd 方式
sudo systemctl restart stocklens
```

---

## 安全提醒

1. **不要用 Flask 开发服务器对外**（`app.run()`），务必用 gunicorn。
   本项目 `app.py` 的 `__main__` 仅供本地调试。
2. 生产环境建议关闭 debug；本项目的 `--debug` 需手动开启。
3. 若开放到公网，建议加**访问控制**（Nginx Basic Auth 或 IP 白名单），
   避免接口被滥用、上游数据源被限流。
4. 定期 `docker system prune` 清理无用镜像。
