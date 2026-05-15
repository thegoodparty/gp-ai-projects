# Stage 1: Builder
FROM python:3.12-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy workspace files
COPY uv.lock ./
COPY pyproject.toml ./
COPY shared/pyproject.toml ./shared/
COPY meeting_qa/pyproject.toml ./meeting_qa/

# Copy source code
COPY shared/ ./shared/
COPY meeting_qa/ ./meeting_qa/

# Install dependencies for meeting-qa package only (no dev deps)
RUN uv sync --frozen --no-dev --package meeting-qa

# Stage 2: Runtime
FROM python:3.12-slim

WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder /app/.venv /app/.venv

# Copy source code
COPY --from=builder /app/shared /app/shared
COPY --from=builder /app/meeting_qa /app/meeting_qa

# Install Lambda Runtime Interface Client into the venv
RUN /app/.venv/bin/pip install --no-cache-dir awslambdaric

# Set environment
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app:/app/meeting_qa"
ENV PYTHONUNBUFFERED=1

# Lambda runtime interface client entrypoint
ENTRYPOINT ["python", "-m", "awslambdaric"]

# Default handler
CMD ["meeting_qa.lambda_handler.handler"]
