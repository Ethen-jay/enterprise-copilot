# -*- coding: utf-8 -*-
# enterprise-copilot 一体化网关镜像（F2）
#
# 封入文档转换所需的两类系统依赖：
#   - weasyprint：渲染 HTML→PDF，依赖 Pango/Cairo/GdkPixbuf 等图形库；
#   - pandoc：markdown→docx 等格式转换的底层引擎。
#
# 构建：  docker build -t enterprise-copilot .
# 运行见 docker-compose.yml（挂载 data/ 持久化 + 注入 .env 配置）。

FROM python:3.11-slim

# weasyprint 图形依赖 + pandoc（文档转换链路）
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libpangocairo-1.0-0 \
        libgdk-pixbuf-2.0-0 \
        libcairo2 \
        libffi8 \
        shared-mime-info \
        pandoc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖（利用 Docker 层缓存），再拷代码
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

COPY . .

EXPOSE 8000

CMD ["uvicorn", "gateway.app:app", "--host", "0.0.0.0", "--port", "8000"]