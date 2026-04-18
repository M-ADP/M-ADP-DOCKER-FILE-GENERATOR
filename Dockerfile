FROM python:3.12-slim AS builder

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
RUN uv pip install --system --no-cache -r pyproject.toml && \
    rm -rf ~/.cache/pip

FROM alpine:3.19 AS tools

ARG NERDCTL_VERSION=2.0.3
RUN apk add --no-cache curl && \
    ARCH=$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/') && \
    curl -fsSL "https://github.com/containerd/nerdctl/releases/download/v${NERDCTL_VERSION}/nerdctl-${NERDCTL_VERSION}-linux-${ARCH}.tar.gz" | \
    tar xzf - -C /usr/local/bin nerdctl

FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Seoul

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/uvicorn /usr/local/bin/uvicorn
COPY --from=tools /usr/local/bin/nerdctl /usr/local/bin/nerdctl
COPY --from=hadolint/hadolint:latest-debian /bin/hadolint /usr/local/bin/hadolint

COPY . .

RUN groupadd -r appgroup && useradd -r -g appgroup appuser && \
    find . -type d -name "__pycache__" -exec rm -rf {} + && \
    find . -name "*.pyc" -delete && \
    chown -R appuser:appgroup /app

USER appuser

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
