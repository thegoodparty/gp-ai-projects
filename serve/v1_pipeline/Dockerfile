FROM python:3.12-slim as builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

RUN apt-get update && apt-get install -y \
    build-essential \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md ./
COPY serve/ ./serve/
COPY shared/ ./shared/

RUN uv sync --frozen --no-dev

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/*

RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "arm64" ] || [ "$ARCH" = "aarch64" ]; then \
        AWS_CLI_ARCH="aarch64"; \
    else \
        AWS_CLI_ARCH="x86_64"; \
    fi && \
    curl "https://awscli.amazonaws.com/awscli-exe-linux-${AWS_CLI_ARCH}.zip" -o "awscliv2.zip" && \
    unzip awscliv2.zip && \
    ./aws/install && \
    rm -rf awscliv2.zip aws

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/serve /app/serve
COPY --from=builder /app/shared /app/shared

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app"
ENV ENVIRONMENT="production"
ENV OPENBLAS_CORETYPE=ARMV8
ENV NUMBA_DISABLE_JIT=1

RUN mkdir -p \
    /app/serve/input \
    /app/serve/data \
    /app/serve/classify/output \
    /app/serve/v1_pipeline/output \
    /app/serve/hierarchical_discovery/output

COPY serve/v1_pipeline/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
