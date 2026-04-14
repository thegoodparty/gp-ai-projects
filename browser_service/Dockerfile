# Stage 1: Builder
FROM python:3.12-slim AS builder

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy workspace files needed for uv sync
COPY uv.lock ./
COPY pyproject.toml ./
COPY shared/pyproject.toml ./shared/
COPY browser_service/pyproject.toml ./browser_service/

# Copy source code
COPY shared/ ./shared/
COPY browser_service/ ./browser_service/

# Install dependencies for this package only
RUN uv sync --frozen --no-dev --package gp-browser-service

# Stage 2: Runtime
FROM python:3.12-slim

WORKDIR /app

# Install system deps for Chromium (Playwright needs these)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libnss3 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libxkbcommon0 \
    libgbm1 \
    libasound2 \
    libatspi2.0-0 \
    libxshmfence1 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libpango-1.0-0 \
    libcairo2 \
    fonts-liberation \
    fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

# Copy venv and source from builder
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/browser_service /app/browser_service
COPY --from=builder /app/shared /app/shared

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app"
ENV PYTHONUNBUFFERED=1

# Create non-root user for security
RUN useradd -m -s /bin/bash appuser && \
    chown -R appuser:appuser /app

USER appuser

# Install Playwright Chromium browser binary (as appuser so cache lands in /home/appuser)
RUN playwright install chromium

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "browser_service.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
