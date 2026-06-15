FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first for layer caching.
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install \
    "django>=5.0" "djangorestframework>=3.15" "sqlglot>=25.0" "pyarrow>=16.0" \
    "polars>=1.0" "celery>=5.4" "redis>=5.0" \
    "psycopg[binary]>=3.2" "gunicorn>=22.0" "duckdb>=1.0"

COPY . .

EXPOSE 8000

# Default command runs the web server; the worker service overrides it.
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
