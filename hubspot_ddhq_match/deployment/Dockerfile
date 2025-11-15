# Stage 1: Builder
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

RUN apt-get update && apt-get install -y build-essential python3-dev

COPY uv.lock ./uv.lock
COPY pyproject.toml ./pyproject.toml
COPY shared/pyproject.toml ./shared/pyproject.toml
COPY hubspot_ddhq_match/pyproject.toml ./hubspot_ddhq_match/pyproject.toml
COPY shared/ ./shared/
COPY hubspot_ddhq_match/ ./hubspot_ddhq_match/

WORKDIR /app/hubspot_ddhq_match
RUN uv sync --frozen --no-dev --package gp-ddhq-matcher
WORKDIR /app

# Stage 2: Runtime
FROM python:3.12-slim

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/hubspot_ddhq_match /app/hubspot_ddhq_match
COPY --from=builder /app/shared /app/shared

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app"
ENV ENVIRONMENT="production"
ENV OPENBLAS_CORETYPE=ARMV8
ENV NUMBA_DISABLE_JIT=1

RUN mkdir -p /app/hubspot_ddhq_match/offline_data /app/hubspot_ddhq_match/output

COPY hubspot_ddhq_match/deployment/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
