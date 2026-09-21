FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 非 root 运行（与宿主机 admin:Users 对应；compose 里也显式指定 user）
# 目录预先创建并授权，便于 logs/ 与 data/ 以可写卷挂载
RUN groupadd -g 1001 app 2>/dev/null || true \
    && useradd -u 1000 -g 1001 -M -s /usr/sbin/nologin app 2>/dev/null || true \
    && mkdir -p /app/logs /app/data \
    && chown -R 1000:1001 /app/logs /app/data
USER 1000:1001

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "4", "--threads", "2", "--timeout", "30", "--no-control-socket", "--worker-tmp-dir", "/tmp", "--access-logfile", "-", "app:app"]
