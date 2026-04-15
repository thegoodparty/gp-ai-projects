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
    && pip install playwright \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js (required for Claude CLI) — verify SHA256 against the
# official SHASUMS256.txt published alongside the tarball so a compromised
# mirror cannot inject a backdoored binary.
RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "x86_64" ]; then NODE_ARCH="x64"; \
    elif [ "$ARCH" = "aarch64" ]; then NODE_ARCH="arm64"; fi && \
    curl -fsSL "https://nodejs.org/dist/v22.16.0/node-v22.16.0-linux-${NODE_ARCH}.tar.xz" -o node.tar.xz && \
    curl -fsSL "https://nodejs.org/dist/v22.16.0/SHASUMS256.txt" -o SHASUMS256.txt && \
    grep " node-v22.16.0-linux-${NODE_ARCH}.tar.xz$" SHASUMS256.txt | sha256sum -c - && \
    tar -xJf node.tar.xz -C /usr/local --strip-components=1 && \
    rm node.tar.xz SHASUMS256.txt

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

# Copy entrypoint
COPY pmf_engine/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Create non-root user (Claude CLI requires non-root for bypassPermissions)
RUN useradd -m -s /bin/bash agent && \
    mkdir -p /workspace /workspace/output /home/agent/.ssh /home/agent/.aws && \
    chown -R agent:agent /workspace /home/agent /app

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
