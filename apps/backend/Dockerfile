FROM python:3.12-slim

WORKDIR /app

# 系统依赖（pdfminer/lxml 编译需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libxml2-dev \
        libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖（利用层缓存；用清华镜像加速，腾讯云访问 PyPI 慢/超时）
COPY apps/backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# 复制后端源码（Flask 极简版，10 个文件；新增模块必须加入列表）
COPY apps/backend/app.py apps/backend/auth.py apps/backend/config.py apps/backend/llm.py \
     apps/backend/parser.py apps/backend/prompts.py apps/backend/run.py \
     apps/backend/store.py apps/backend/mailer.py apps/backend/reset_password_cli.py ./

# 数据与日志目录（卷管理交给 docker-compose）
RUN mkdir -p /app/data /app/logs

EXPOSE 8000

# Flask + gunicorn，宝塔/Docker 通用，无 ASGI/WSGI 坑。
# GUNICORN_WORKERS 可通过 docker-compose 的 environment 覆盖（默认 4，支持多用户并发）。
ENV GUNICORN_WORKERS=4
CMD ["sh", "-c", "gunicorn -w ${GUNICORN_WORKERS} -b 0.0.0.0:8000 --timeout 1200 app:app"]
