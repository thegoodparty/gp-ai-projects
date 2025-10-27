# Build Notes - ARM64/Graviton

## Why ARM64?

The pipeline is configured to build for **ARM64 (Graviton)** by default when deploying to ECR for:

- **20% cost savings** compared to x86_64
- Same performance characteristics for Python workloads
- Better power efficiency

## Building

### Build and Push to ECR (ARM64)
```bash
AWS_PROFILE=work PUSH_TO_ECR=true ./serve/v1_pipeline/build.sh dev
```

### Build Locally (auto-detect architecture)
```bash
./serve/v1_pipeline/build.sh dev
```

### Override Platform
```bash
# Build for x86_64 if needed
PLATFORM=linux/amd64 PUSH_TO_ECR=true ./serve/v1_pipeline/build.sh dev

# Build multi-arch (slower)
PLATFORM=linux/arm64,linux/amd64 PUSH_TO_ECR=true ./serve/v1_pipeline/build.sh dev
```

## Fargate Pricing (us-west-2)

**ARM64 (Graviton):**
- 4 vCPU, 16GB RAM: ~$0.20/hour
- Typical 10-minute pipeline run: ~$0.03

**x86_64 (Intel/AMD):**
- 4 vCPU, 16GB RAM: ~$0.25/hour
- Typical 10-minute pipeline run: ~$0.04

**Annual Savings** (100 runs/day):
- ARM64: $1,095/year
- x86_64: $1,460/year
- **Savings: $365/year (25% reduction)**

## Dockerfile Optimizations for ARM64

The Dockerfile includes ARM64-specific optimizations:

```dockerfile
# Line 45: BLAS optimization for ARM processors
ENV OPENBLAS_CORETYPE=ARMV8

# Line 46: Disable JIT for ARM compatibility
ENV NUMBA_DISABLE_JIT=1

# Lines 27-36: Auto-detect architecture for AWS CLI
RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "arm64" ] || [ "$ARCH" = "aarch64" ]; then \
        AWS_CLI_ARCH="aarch64"; \
    else \
        AWS_CLI_ARCH="x86_64"; \
    fi
```

## Troubleshooting

### "manifest does not contain descriptor matching platform"

This error means the Docker image was built for the wrong architecture:

```bash
# Check image architecture
aws ecr describe-images \
  --repository-name gp-ai-projects \
  --image-ids imageTag=serve-analyze-dev \
  --query 'imageDetails[0]'

# Rebuild for ARM64
AWS_PROFILE=work PLATFORM=linux/arm64 PUSH_TO_ECR=true \
  ./serve/v1_pipeline/build.sh dev
```

### Build Time

ARM64 builds on x86_64 machines use QEMU emulation and are **slower**:
- Native ARM64 build: ~2-3 minutes
- Cross-platform build (x86→ARM): ~8-12 minutes

For faster builds, use an ARM64 build machine (e.g., AWS Graviton EC2 instance or M1/M2 Mac).
