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

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/broker /app/broker

# Install Chromium + its OS deps. `--with-deps` shells out to apt to install
# the system libraries Chromium needs (libnss3, libatk-bridge2.0-0, fonts,
# etc.). Run as root before switching to the broker user.
RUN /app/.venv/bin/playwright install --with-deps chromium

RUN useradd -m -s /bin/bash broker && \
    chown -R broker:broker /app /ms-playwright

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app"
ENV PYTHONUNBUFFERED=1

USER broker

EXPOSE 8080

CMD ["uvicorn", "broker.main:app", "--host", "0.0.0.0", "--port", "8080"]
