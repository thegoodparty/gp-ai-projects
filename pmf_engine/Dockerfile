# Stage 1: Builder
FROM python:3.12-slim AS builder

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install git for potential repo cloning
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Copy workspace files
COPY uv.lock ./
COPY pyproject.toml ./
COPY shared/pyproject.toml ./shared/
COPY pmf_engine/pyproject.toml ./pmf_engine/

# Copy source code
COPY shared/ ./shared/
COPY pmf_engine/ ./pmf_engine/

# Install dependencies for this package only
RUN uv sync --frozen --no-dev --package gp-pmf-engine

# Stage 2: Runtime
FROM python:3.12-slim

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    openssh-client \
    curl \
    unzip \
    ca-certificates \
    xz-utils \
    poppler-utils \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js (required for Claude CLI) — verify SHA256 against the
# official SHASUMS256.txt published alongside the tarball so a compromised
# mirror cannot inject a backdoored binary.
RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "x86_64" ]; then NODE_ARCH="x64"; \
    elif [ "$ARCH" = "aarch64" ]; then NODE_ARCH="arm64"; fi && \
    NODE_FILE="node-v22.16.0-linux-${NODE_ARCH}.tar.xz" && \
    curl -fsSL "https://nodejs.org/dist/v22.16.0/${NODE_FILE}" -o "${NODE_FILE}" && \
    curl -fsSL "https://nodejs.org/dist/v22.16.0/SHASUMS256.txt" -o SHASUMS256.txt && \
    grep " ${NODE_FILE}$" SHASUMS256.txt | sha256sum -c - && \
    tar -xJf "${NODE_FILE}" -C /usr/local --strip-components=1 && \
    rm "${NODE_FILE}" SHASUMS256.txt

# Install Claude CLI (required for Claude SDK harness) — pin the version so
# image rebuilds are reproducible and a compromised latest release cannot
# silently land in production.
RUN npm install -g @anthropic-ai/claude-code@2.1.109

# Install AWS CLI (for S3 artifact uploads if needed by agent)
RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "x86_64" ]; then \
        curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"; \
    elif [ "$ARCH" = "aarch64" ]; then \
        curl "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o "awscliv2.zip"; \
    fi && \
    unzip awscliv2.zip && \
    ./aws/install && \
    rm -rf awscliv2.zip aws

# Copy virtual environment from builder
COPY --from=builder /app/.venv /app/.venv

# Copy source code
COPY --from=builder /app/shared /app/shared
COPY --from=builder /app/pmf_engine /app/pmf_engine

# Sandbox egress guard: PYTHONPATH=/app is set below, so every python3 the
# agent spawns auto-imports this and installs the socket-level guard when
# BROKER_URL is present.
COPY pmf_engine/sitecustomize.py /app/sitecustomize.py

# Sandbox egress guard: shadow curl/wget with fast-fail shims so direct
# network calls fail in <1s with an instructive message instead of hanging
# ~30s against the network-quarantined container. /usr/local/bin precedes
# /usr/bin in PATH; the real binaries remain at /usr/bin and nothing at
# runtime needs them. Must come AFTER all build-time curl uses above.
RUN SANDBOX_MSG="Sandboxed environment: this container has NO direct network egress. Direct network calls (urllib, requests, httpx, curl, wget, raw socket) cannot reach the internet and will fail. The ONLY way to reach a URL is the broker-proxied tools you were given: pmf_runtime.http.head(url) / pmf_runtime.http.get(url) / pmf_runtime.http.download(url) for web pages, and the WebSearch tool for discovery. Use those instead. See /workspace/SANDBOX.md." && \
    for bin in curl wget; do \
        printf '#!/bin/sh\necho "%s" >&2\nexit 1\n' "$SANDBOX_MSG" > "/usr/local/bin/$bin" && \
        chmod +x "/usr/local/bin/$bin"; \
    done

# Copy entrypoint
COPY pmf_engine/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Create non-root user (Claude CLI requires non-root for bypassPermissions)
RUN useradd -m -s /bin/bash agent && \
    mkdir -p /workspace /workspace/output /home/agent/.ssh /home/agent/.aws && \
    chown -R agent:agent /workspace /home/agent /app

# Make pmf_runtime importable as a top-level package so agent scripts
# can do `from pmf_runtime import databricks as sql` from any CWD.
RUN ln -s /app/pmf_engine/runner/pmf_runtime /app/.venv/lib/python3.12/site-packages/pmf_runtime

# Set environment
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app"
ENV PYTHONUNBUFFERED=1
ENV WORKSPACE_DIR="/workspace"
ENV HOME="/home/agent"

# Switch to non-root user
USER agent
WORKDIR /workspace

ENTRYPOINT ["/app/entrypoint.sh"]
