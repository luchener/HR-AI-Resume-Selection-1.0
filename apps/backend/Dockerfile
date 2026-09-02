FROM python:3.12-slim

WORKDIR /app

# 更换为腾讯云镜像源加速 apt 安装
RUN sed -i 's|deb.debian.org|mirrors.tencent.com|g' /etc/apt/sources.list.d/debian.sources || \
    sed -i 's|deb.debian.org|mirrors.tencent.com|g' /etc/apt/sources.list && \
    apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libxml2-dev \
        libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖（利用层缓存；用清华镜像加速，腾讯云访问 PyPI 慢/超时）
COPY apps/backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# 创建非 root 用户（uid/gid 1000，与前端保持一致）
RUN groupadd -r appgroup -g 1000 && \
    useradd -r -g appgroup -u 1000 -d /app -s /sbin/nologin appuser

# 复制后端源码（通配符；新增模块自动生效，无需修改 Dockerfile）
COPY apps/backend/*.py ./

# Copy the tools/ package (subdirectory, not matched by the *.py glob above)
COPY apps/backend/tools/ ./tools/

# 数据与日志目录（卷管理交给 docker-compose）
RUN mkdir -p /app/data /app/logs && \
    chown -R appuser:appgroup /app

# 切换到非 root 用户
USER appuser

EXPOSE 8000

# Flask + gunicorn，宝塔/Docker 通用，无 ASGI/WSGI 坑。
# GUNICORN_WORKERS 可通过 docker-compose 的 environment 覆盖（默认 4，支持多用户并发）。
ENV GUNICORN_WORKERS=4
CMD ["sh", "-c", "gunicorn -w ${GUNICORN_WORKERS} -b 0.0.0.0:8000 --timeout 1200 app:app"]
