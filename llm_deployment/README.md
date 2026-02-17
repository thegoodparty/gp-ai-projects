# RunPod Deployment: Qwen3-Coder-Next-FP8

Deploy Qwen3-Coder-Next-FP8 (80B MoE, FP8) on RunPod with vLLM serving an OpenAI-compatible API.

## Quick Start

```bash
# 1. Copy env and add your RunPod API key + vLLM API key
cp llm_deployment/.env.example llm_deployment/.env
# Edit .env — RUNPOD_API_KEY is required, VLLM_API_KEY recommended

# 2. Launch pod and wait for vLLM to be ready (~10 min first time)
uv run llm_deployment/pod.py launch --wait

# 3. Test it
uv run llm_deployment/pod.py test
```

## Prerequisites

- **RunPod account** with billing set up
- **RunPod API key**: Settings > API Keys at https://www.runpod.io/console/user/settings
- **SSH key uploaded to RunPod** (for SSH access to pod): uploaded via `updateUserSettings` GraphQL mutation
- **uv** (Python package manager) — all deps are in the workspace root `pyproject.toml` (`httpx`, `python-dotenv`)

## Pod Management

```bash
uv run llm_deployment/pod.py launch [--wait]  # create pod, optionally wait until API ready
uv run llm_deployment/pod.py wait              # poll until the vLLM API is responding
uv run llm_deployment/pod.py status            # show all pods with GPU type and uptime
uv run llm_deployment/pod.py test              # hit /v1/models + /v1/chat/completions
uv run llm_deployment/pod.py stop              # pause billing (volume preserved)
uv run llm_deployment/pod.py start             # resume a stopped pod
uv run llm_deployment/pod.py url               # print the API base URL
uv run llm_deployment/pod.py destroy           # terminate pod and delete volume
```

## Lifecycle & Billing

### First-time setup
```bash
uv run llm_deployment/pod.py launch --wait   # ~10 min (image pull + model download + warmup)
```

### Daily workflow
```bash
# Morning: resume pod (~5 min, model cached on volume, needs warmup)
uv run llm_deployment/pod.py start
uv run llm_deployment/pod.py wait

# Use the API...

# Evening: pause billing
uv run llm_deployment/pod.py stop
```

### Tear down completely
```bash
uv run llm_deployment/pod.py destroy   # deletes pod + volume, prompts for confirmation
```

### Cost breakdown

| State | Cost | What you pay for |
|-------|------|------------------|
| **Running** | ~$5.02/hr (B200) | GPU compute |
| **Stopped** | ~$0.10/GB/month (~$20/mo for 200GB) | Volume storage only |
| **Destroyed** | $0 | Nothing |

## SSH Access

SSH into the pod for debugging, logs, or manual operations:

```bash
ssh <pod-id>-<session>@ssh.runpod.io -i ~/.ssh/id_ed25519
```

The SSH command with the full session ID is shown in the RunPod dashboard under Pods > (click pod) > Connect > SSH.

### Useful SSH commands
```bash
nvidia-smi                    # GPU memory usage and utilization
ps aux | grep vllm            # check if vLLM is running
cat /proc/1/fd/1 | tail -50   # view vLLM container logs
tail -f /tmp/vllm.log         # follow logs (if started with nohup redirect)

# Restart vLLM with different args (e.g., add API key)
pkill -f vllm
nohup vllm serve Qwen/Qwen3-Coder-Next-FP8 \
  --dtype auto --max-model-len 32768 --trust-remote-code \
  --enable-prefix-caching --gpu-memory-utilization 0.92 \
  --api-key YOUR_KEY --port 8000 > /tmp/vllm.log 2>&1 &
```

## Hardware

| Resource | Spec |
|----------|------|
| GPU | 1x NVIDIA B200 (180GB HBM3e) |
| Model size | ~75GB (FP8, 80B MoE) |
| Free VRAM for KV cache | ~83GB |
| KV cache capacity | 911K tokens (~100 concurrent 32k requests) |
| Volume | 200GB persistent (model cache survives stop/start) |

### GPU options (configurable via `GPU_TYPE_ID` in .env)

| GPU ID | VRAM | Price/hr | Notes |
|--------|------|----------|-------|
| `NVIDIA B200` | 180GB | ~$5.02 | Current default. Plenty of headroom. |
| `NVIDIA H200 NVL` | 143GB | ~$3.39 | Good balance. ~60GB for KV cache. |
| `NVIDIA H200` (SXM) | 141GB | ~$3.59 | Similar to NVL. |
| `NVIDIA H100 80GB HBM3` | 80GB | ~$2.69 | Too tight — model fills VRAM, minimal KV cache. |

Availability varies. The script uses `cloudType: ALL` to search both secure and community clouds.

## API Usage

The vLLM server exposes an OpenAI-compatible API at:
```
https://<pod-id>-8000.proxy.runpod.net/v1
```

### curl

```bash
BASE_URL=$(uv run llm_deployment/pod.py url)

# List models
curl $BASE_URL/models -H "Authorization: Bearer $VLLM_API_KEY"

# Chat completion
curl $BASE_URL/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $VLLM_API_KEY" \
  -d '{
    "model": "Qwen/Qwen3-Coder-Next-FP8",
    "messages": [{"role": "user", "content": "Write a Python fibonacci function"}],
    "max_tokens": 500
  }'
```

### Python (OpenAI SDK)

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://<pod-id>-8000.proxy.runpod.net/v1",
    api_key="your-vllm-api-key",  # from VLLM_API_KEY in .env
)

response = client.chat.completions.create(
    model="Qwen/Qwen3-Coder-Next-FP8",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(response.choices[0].message.content)
```

### Python (httpx)

```python
import httpx

resp = httpx.post(
    "https://<pod-id>-8000.proxy.runpod.net/v1/chat/completions",
    headers={"Authorization": "Bearer your-vllm-api-key"},
    json={
        "model": "Qwen/Qwen3-Coder-Next-FP8",
        "messages": [{"role": "user", "content": "Hello!"}],
        "max_tokens": 200,
    },
    timeout=60,
)
print(resp.json()["choices"][0]["message"]["content"])
```

## vLLM Configuration

| Flag | Value | Purpose |
|------|-------|---------|
| `--model` | Qwen/Qwen3-Coder-Next-FP8 | Model to serve |
| `--dtype` | auto | Auto-detect dtype (FP8) |
| `--max-model-len` | 32768 | Max context length |
| `--trust-remote-code` | — | Required for Qwen models |
| `--enable-prefix-caching` | — | Cache common prefixes for speed |
| `--gpu-memory-utilization` | 0.92 | Use 92% of VRAM |
| `--api-key` | from `VLLM_API_KEY` | Bearer token auth (optional) |

These are configured in `pod.py` `cmd_launch()` and passed as `dockerArgs` to RunPod.

## Startup Timeline

What happens when a pod launches (first boot):

| Phase | Duration | What happens |
|-------|----------|--------------|
| Image pull | ~1 min | Pulls `vllm/vllm-openai:latest` |
| Model download | ~2 min | Downloads ~80GB from HuggingFace to volume |
| Weight loading | ~1 min | Loads FP8 weights into GPU memory |
| torch.compile | ~1.5 min | Compiles optimized CUDA kernels |
| DeepGEMM warmup | ~3 min | Warms up 1304 MoE GEMM kernels |
| FlashInfer autotune | ~5 sec | Autotunes attention kernels |
| CUDA graph capture | ~12 sec | Captures 51 execution graphs |
| **Total** | **~8-10 min** | |

Subsequent starts (after `stop` → `start`) skip the model download (~2 min faster) since it's cached on the volume. Warmup steps still run.

## Environment Variables

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `RUNPOD_API_KEY` | Yes | — | RunPod account API key |
| `HF_TOKEN` | No | — | HuggingFace token (for gated models) |
| `MODEL_NAME` | No | `Qwen/Qwen3-Coder-Next-FP8` | Model to serve |
| `GPU_TYPE_ID` | No | `NVIDIA H200 NVL` | GPU type (see GPU options above) |
| `GPU_COUNT` | No | `1` | Number of GPUs |
| `VOLUME_SIZE_GB` | No | `200` | Persistent volume size |
| `VLLM_PORT` | No | `8000` | vLLM server port |
| `VLLM_API_KEY` | No | — | Bearer token for vLLM API auth |

## Using with Claude Code (LiteLLM Proxy)

Claude Code speaks the Anthropic Messages API (`/v1/messages`), but vLLM speaks the OpenAI Chat Completions API (`/v1/chat/completions`). LiteLLM proxy translates between them.

### Start the proxy
```bash
# Install litellm if needed
pip install litellm

# Start proxy (translates Anthropic → OpenAI format)
litellm --config llm_deployment/litellm_config.yaml --port 4001
```

### Run Claude Code with Qwen3-Coder
```bash
ANTHROPIC_BASE_URL=http://localhost:4001 ANTHROPIC_MODEL=qwen3-coder ANTHROPIC_AUTH_TOKEN=fake-key claude
```

### How it works
```
Claude Code (Anthropic Messages API)
    ↓ POST /v1/messages
LiteLLM Proxy (localhost:4001)
    ↓ Translates → POST /v1/chat/completions
vLLM on RunPod (OpenAI-compatible API)
    ↓ Response
LiteLLM Proxy
    ↓ Translates back → Anthropic response format
Claude Code
```

## Kimi K2.5 NVFP4 on B200 (4x GPU)

### The Problem

Running Kimi-K2.5-NVFP4 on RunPod B200s requires vLLM 0.15.1+ (for model support), but:
- Standard vLLM Docker images (`vllm/vllm-openai:v0.15.x`) fail with **Error 803** on RunPod B200 due to CUDA driver mismatch (images use CUDA 12.8, RunPod has driver 580.x / CUDA 13.1)
- The NVIDIA NGC container (`nvcr.io/nvidia/vllm:26.01-py3`) ships vLLM 0.13.0 (too old for Kimi K2.5)

### The Solution

Use the NGC container as the base (it has CUDA forward compatibility for Blackwell), then upgrade vLLM in-place with two critical fixes:

1. **`pip install vllm==0.15.1`** — upgrades vLLM but also downgrades PyTorch from NVIDIA's 2.10.0a0 (CUDA 13.1) to standard 2.9.1 (CUDA 12.8)
2. **`pip uninstall flash-attn -y`** — the pre-installed `flash_attn_2_cuda.so` was compiled against the old PyTorch, causing ABI mismatch (`undefined symbol: _ZN3c104cuda29c10_cuda_check_implementationEiPKcS2_jb`). Blackwell uses FlashInfer MLA, not flash-attn, so removing it is safe.
3. **`FLASHINFER_DISABLE_VERSION_CHECK=1`** — vLLM 0.15.1 pulls in `flashinfer-python` 0.6.1, but the NGC container has `flashinfer-cubin` 0.6.0 (NVIDIA custom build for CUDA 13.1). This env var bypasses the version check. The minor version difference (0.6.0 → 0.6.1) is compatible.

The startup command in `pod.py`:
```bash
pip install vllm==0.15.1 && \
pip uninstall flash-attn -y && \
FLASHINFER_DISABLE_VERSION_CHECK=1 \
python3 -m vllm.entrypoints.openai.api_server \
  --model nvidia/Kimi-K2.5-NVFP4 \
  --tensor-parallel-size 4 \
  --max-model-len 32768 \
  --enable-auto-tool-choice --tool-call-parser kimi_k2 --reasoning-parser kimi_k2 \
  --trust-remote-code
```

### .env config for Kimi K2.5

```
MODEL_NAME=nvidia/Kimi-K2.5-NVFP4
GPU_TYPE_ID=NVIDIA B200
GPU_COUNT=4
VOLUME_SIZE_GB=700
```

### Hardware requirements

| Resource | Spec |
|----------|------|
| GPU | 4x NVIDIA B200 (720GB total VRAM) |
| Model size | ~119 safetensor shards, NVFP4 quantized |
| KV cache dtype | FP8 (e4m3) — auto-selected |
| Attention backend | FlashInfer MLA (with TensorRT-LLM kernels) |
| MoE backend | FLASHINFER_TRTLLM |
| Volume | 700GB (model checkpoint ~600GB on disk) |

### Startup timeline (first boot)

| Phase | Duration |
|-------|----------|
| Image pull + pip install vllm==0.15.1 | ~2 min |
| Model download (119 shards to volume) | ~5 min |
| Weight loading into 4x B200 VRAM | ~3 min |
| torch.compile + CUDA graph capture | ~3 min |
| MoE kernel warmup | ~3 min |
| **Total first boot** | **~16 min** (observed) |
| **Subsequent boots (stop→start)** | **~10 min** (model cached on volume, pip install still runs) |

### Benchmark results (4x B200, NVFP4, FP8 KV cache)

Sequential (single request):

| Test | Tokens | Time | tok/s |
|------|--------|------|-------|
| short | 50 | 0.68s | 73.6 |
| medium | 500 | 4.56s | 109.8 |
| long | 2000 | 17.66s | 113.2 |
| reasoning | 403 | 3.71s | 108.7 |
| code | 1000 | 8.82s | 113.3 |
| creative | 500 | 4.67s | 107.0 |
| tool_use | 97 | 0.93s | 104.7 |
| multilingual | 300 | 2.76s | 108.8 |

Streaming TTFB: **0.17s** average

Concurrent throughput scaling:

| Concurrency | Aggregate tok/s | Scaling |
|-------------|-----------------|---------|
| x1 | 105 | baseline |
| x2 | 219 | 2.1x |
| x4 | 412 | 3.9x |
| x8 | 734 | 7.0x |
| x16 | 1,268 | 12.1x |

Run benchmarks: `uv run llm_deployment/benchmark.py --concurrency 2 4 8 16`

### Cost

| State | Cost |
|-------|------|
| Running | ~$20/hr (4x B200) |
| Stopped | ~$0.10/GB/month (~$70/mo for 700GB volume) |

## Known RunPod Limitations (as of Feb 2026)

### B200 GPUs: standard vLLM images don't work

RunPod's B200 (Blackwell) machines run host driver **580.x / CUDA runtime 13.1**. Standard vLLM Docker images fail with Error 803. **Workaround:** Use the NGC container (`nvcr.io/nvidia/vllm:26.01-py3`) which has CUDA forward compatibility, then upgrade vLLM in-place (see Kimi K2.5 section above).

**Images that fail on RunPod B200:**
- `vllm/vllm-openai:latest`, `v0.15.0`, `v0.15.1`, `v0.15.1-cu130`, `nightly`

See [vllm#33447](https://github.com/vllm-project/vllm/issues/33447).

### H200 GPUs: availability

H200 NVL and H200 SXM are frequently out of stock on RunPod.

## Files

| File | Purpose |
|------|---------|
| `pod.py` | Pod lifecycle management (launch, wait, status, stop, start, destroy, test, url) |
| `litellm_config.yaml` | LiteLLM proxy config pointing at RunPod vLLM |
| `.env.example` | Template for env vars |
| `.env` | Your config with secrets (gitignored) |
| `.pod_id` | Auto-created — tracks active pod ID (gitignored) |
| `README.md` | This file |
