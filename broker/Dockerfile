FROM python:3.12-slim AS builder

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY uv.lock ./
COPY pyproject.toml ./
COPY broker/pyproject.toml ./broker/

COPY broker/ ./broker/

RUN uv sync --frozen --no-dev --package gp-broker

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/broker /app/broker

RUN useradd -m -s /bin/bash broker && \
    chown -R broker:broker /app

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app"
ENV PYTHONUNBUFFERED=1

USER broker

EXPOSE 8080

CMD ["uvicorn", "broker.main:app", "--host", "0.0.0.0", "--port", "8080"]
