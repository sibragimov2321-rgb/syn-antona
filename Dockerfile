FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
COPY config ./config
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini
COPY phase4i-prospective-lock.json phase4i-warmup.json.gz ./
RUN pip install --no-cache-dir .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
