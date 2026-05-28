# Stage 1: Builder
FROM python:3.12-slim AS builder

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install git for cloning repos
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Copy workspace files
COPY uv.lock ./
COPY pyproject.toml ./
COPY shared/pyproject.toml ./shared/
COPY engineer_agent/pyproject.toml ./engineer_agent/

# Copy source code
COPY shared/ ./shared/
COPY engineer_agent/ ./engineer_agent/

# Install dependencies for this package only
RUN uv sync --frozen --no-dev --package gp-engineer-agent

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
    procps \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js via official binary (avoids python3 dependency issues)
RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "x86_64" ]; then NODE_ARCH="x64"; \
    elif [ "$ARCH" = "aarch64" ]; then NODE_ARCH="arm64"; fi && \
    curl -fsSL "https://nodejs.org/dist/v22.16.0/node-v22.16.0-linux-${NODE_ARCH}.tar.xz" -o node.tar.xz && \
    tar -xJf node.tar.xz -C /usr/local --strip-components=1 && \
    rm node.tar.xz

# Install Claude CLI globally
RUN npm install -g @anthropic-ai/claude-code

# Install AWS CLI (multi-arch)
RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "x86_64" ]; then \
        curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"; \
    elif [ "$ARCH" = "aarch64" ]; then \
        curl "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o "awscliv2.zip"; \
    fi && \
    unzip awscliv2.zip && \
    ./aws/install && \
    rm -rf awscliv2.zip aws

# Install GitHub CLI (multi-arch)
RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "x86_64" ]; then GH_ARCH="amd64"; \
    elif [ "$ARCH" = "aarch64" ]; then GH_ARCH="arm64"; fi && \
    curl -sL "https://github.com/cli/cli/releases/download/v2.63.2/gh_2.63.2_linux_${GH_ARCH}.tar.gz" | tar xz && \
    mv gh_2.63.2_linux_${GH_ARCH}/bin/gh /usr/local/bin/ && \
    rm -rf gh_2.63.2_linux_${GH_ARCH}

# Copy virtual environment from builder
COPY --from=builder /app/.venv /app/.venv

# Copy source code
COPY --from=builder /app/shared /app/shared
COPY --from=builder /app/engineer_agent /app/engineer_agent

# Copy entrypoint
COPY engineer_agent/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Create non-root user (Claude CLI refuses to run as root with bypassPermissions)
RUN useradd -m -s /bin/bash agent && \
    mkdir -p /workspace /home/agent/.ssh /home/agent/.aws && \
    chown -R agent:agent /workspace /home/agent /app

# Configure git for the agent user
RUN git config --system user.email "gp-bot@goodparty.org" && \
    git config --system user.name "GP Bot"

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
