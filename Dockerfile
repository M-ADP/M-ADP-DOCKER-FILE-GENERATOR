FROM python:3.12-slim AS builder

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
RUN uv pip install --system --no-cache -r pyproject.toml && \
    rm -rf ~/.cache/pip

FROM moby/buildkit:latest AS tools

FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Seoul

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/uvicorn /usr/local/bin/uvicorn
COPY --from=tools /usr/bin/buildctl /usr/local/bin/buildctl
COPY --from=hadolint/hadolint:latest-debian /bin/hadolint /usr/local/bin/hadolint

COPY . .

RUN groupadd -r appgroup && useradd -r -g appgroup appuser && \
    find . -type d -name "__pycache__" -exec rm -rf {} + && \
    find . -name "*.pyc" -delete && \
    chown -R appuser:appgroup /app

USER appuser

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
