# syntax=docker/dockerfile:1

FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app
WORKDIR /app
COPY pyproject.toml ./
COPY app ./app
RUN pip install --upgrade pip \
    && pip install fastapi==0.115.6 "uvicorn[standard]==0.34.0" \
       asyncpg==0.30.0 pydantic==2.10.4
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS tests
COPY tests ./tests
RUN pip install pytest==8.3.4 pytest-asyncio==0.25.0 httpx==0.28.1
CMD ["python", "-m", "pytest", "-v"]
