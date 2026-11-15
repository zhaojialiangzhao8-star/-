# 单容器：后端 + 静态前端，零外部依赖（数据可挂载持久化）
FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/app/data \
    STATIC_DIR=/app/web \
    TZ=Asia/Shanghai

# 依赖先装，利用层缓存（numpy 是 11 个策略插件要用的）
RUN pip install --no-cache-dir \
      "fastapi>=0.115" "uvicorn[standard]>=0.30" "pydantic>=2.7" \
      "polars>=1.0,<2" "pyarrow>=16.0" numpy

COPY backend/app /app/app
COPY scripts /app/scripts
# 前端必须用「构建产物」，不是空的 web/ 目录：
#   cd frontend && npm ci && npm run build   （产物在 frontend/dist）
COPY frontend/dist /app/web

# 首次启动没有数据时自动造一份样本（生产环境用挂载覆盖 /app/data）
RUN python /app/scripts/gen_data.py

EXPOSE 3018
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:3018/api/health',timeout=3)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3018"]
