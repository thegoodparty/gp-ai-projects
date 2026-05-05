# RunPod vLLM Deployment

Deploy any vLLM-compatible model on RunPod GPUs with an OpenAI-compatible API. Model, GPU type, and GPU count are all configurable via `.env`.

Default: **Kimi K2.5 NVFP4** (1T MoE, NVFP4 quantized) on **4x NVIDIA B200**.

## Quick Start

```bash
# 1. Copy env and add your RunPod API key + vLLM API key
cp llm_deployment/.env.example llm_deployment/.env
# Edit .env — RUNPOD_API_KEY is required, VLLM_API_KEY recommended

# 2. Launch pod and wait for vLLM to be ready (~16 min first time)
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
uv run llm_deployment/pod.py launch --wait   # ~16 min (image pull + pip install + model download + warmup)
```

### Daily workflow
```bash
# Morning: resume pod (~10 min, model cached on volume, needs warmup)
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

### Cost (Kimi K2.5 default config: 4x B200)

| State | Cost | What you pay for |
|-------|------|------------------|
| **Running** | ~$20/hr | GPU compute (4x B200 @ $4.99/hr) |
| **Stopped** | ~$0.10/GB/month (~$70/mo for 700GB) | Volume storage only |
| **Destroyed** | $0 | Nothing |

## Environment Variables

All model and hardware settings are configurable via `.env`:

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `RUNPOD_API_KEY` | Yes | — | RunPod account API key |
| `HF_TOKEN` | No | — | HuggingFace token (for gated models) |
| `MODEL_NAME` | No | `nvidia/Kimi-K2.5-NVFP4` | Model to serve |
| `GPU_TYPE_ID` | No | `NVIDIA B200` | GPU type (see GPU options below) |
| `GPU_COUNT` | No | `4` | Number of GPUs (sets `--tensor-parallel-size`) |
| `VOLUME_SIZE_GB` | No | `700` | Persistent volume size |
| `VLLM_PORT` | No | `8000` | vLLM server port |
| `VLLM_API_KEY` | No | — | Bearer token for vLLM API auth |

## GPU Options

Configurable via `GPU_TYPE_ID` and `GPU_COUNT` in `.env`.

| GPU ID | VRAM | Price/hr | Notes |
|--------|------|----------|-------|
| `NVIDIA B200` | 180GB | ~$4.99 | Default. Native NVFP4 on Blackwell. |
| `NVIDIA H200` (SXM) | 141GB | ~$3.59 | NVFP4 via Marlin W4A16 fallback — same quality, lower throughput. |
| `NVIDIA H200 NVL` | 143GB | ~$3.39 | Same as H200 SXM. Secure cloud only. |
| `NVIDIA H100 80GB HBM3` | 80GB | ~$2.69 | Marlin fallback. Need 8+ GPUs for large models. |

For Kimi K2.5 NVFP4 (~600GB on disk):
- **4x B200** (720GB VRAM) — recommended, native FP4
- **8x H200** (1128GB VRAM) — fallback, Marlin W4A16 dequant at runtime

Availability varies. The script uses `cloudType: ALL` to search both secure and community clouds.

## Hardware (Kimi K2.5 default)

| Resource | Spec |
|----------|------|
| GPU | 4x NVIDIA B200 (720GB total VRAM) |
| Model size | ~119 safetensor shards, NVFP4 quantized |
| KV cache dtype | FP8 (e4m3) — auto-selected |
| Attention backend | FlashInfer MLA (with TensorRT-LLM kernels) |
| MoE backend | FLASHINFER_TRTLLM |
| Volume | 700GB (model checkpoint ~600GB on disk) |

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

# Restart vLLM with different args (faster than destroy + launch)
pkill -f vllm
FLASHINFER_DISABLE_VERSION_CHECK=1 nohup python3 -m vllm.entrypoints.openai.api_server \
  --model nvidia/Kimi-K2.5-NVFP4 --host 0.0.0.0 --port 8000 \
  --tensor-parallel-size 4 --max-model-len 131072 \
  --enable-auto-tool-choice --tool-call-parser kimi_k2 --reasoning-parser kimi_k2 \
  --trust-remote-code --api-key YOUR_KEY > /tmp/vllm.log 2>&1 &
```

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
    "model": "nvidia/Kimi-K2.5-NVFP4",
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
    model="nvidia/Kimi-K2.5-NVFP4",
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
        "model": "nvidia/Kimi-K2.5-NVFP4",
        "messages": [{"role": "user", "content": "Hello!"}],
        "max_tokens": 200,
    },
    timeout=60,
)
print(resp.json()["choices"][0]["message"]["content"])
```

## Using with Claude Code (LiteLLM Proxy)

Claude Code speaks the Anthropic Messages API (`/v1/messages`), but vLLM speaks the OpenAI Chat Completions API (`/v1/chat/completions`). LiteLLM proxy sits in between and translates the formats.

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

### Step 1: Install LiteLLM

```bash
pip install litellm
```

### Step 2: Update litellm config with your pod ID

After launching a pod, update `litellm_config.yaml` with the pod ID and your vLLM API key:

```bash
# Get your pod ID
cat llm_deployment/.pod_id

# Edit litellm_config.yaml — replace <pod-id> with the actual pod ID
# and set your VLLM_API_KEY
```

The config should look like:
```yaml
model_list:
  - model_name: kimi-k2.5
    litellm_params:
      model: openai/nvidia/Kimi-K2.5-NVFP4
      api_base: https://YOUR_POD_ID-8000.proxy.runpod.net/v1
      api_key: your-vllm-api-key

litellm_settings:
  drop_params: true
```

### Step 3: Start the proxy

```bash
litellm --config llm_deployment/litellm_config.yaml --port 4001
```

Leave this running in a terminal. It will log all requests being proxied.

### Step 4: Run Claude Code against the self-hosted model

In a new terminal:

```bash
ANTHROPIC_BASE_URL=http://localhost:4001 ANTHROPIC_AUTH_TOKEN=fake-key claude --model kimi-k2.5
```

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_BASE_URL` | Points Claude Code at LiteLLM instead of Anthropic's API |
| `ANTHROPIC_AUTH_TOKEN` | Any non-empty string (LiteLLM doesn't validate it unless you set a master key) |
| `--model` | Must match the `model_name` in `litellm_config.yaml` |

Claude Code will now use Kimi K2.5 on your RunPod pod for all completions instead of Anthropic's API.

## Startup Timeline

| Phase | Duration |
|-------|----------|
| Image pull + pip install vllm==0.15.1 | ~2 min |
| Model download (119 shards to volume) | ~5 min |
| Weight loading into 4x B200 VRAM | ~3 min |
| torch.compile + CUDA graph capture | ~3 min |
| MoE kernel warmup | ~3 min |
| **Total first boot** | **~16 min** (observed) |
| **Subsequent boots (stop→start)** | **~10 min** (model cached on volume, pip install still runs) |

## Benchmark Results (4x B200, Kimi K2.5 NVFP4)

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

## B200 Blackwell Workarounds

Running on RunPod B200s requires the NGC container because standard vLLM Docker images fail with Error 803 (CUDA driver mismatch — images use CUDA 12.8, RunPod has driver 580.x / CUDA 13.1).

The startup command in `pod.py` uses this workaround:

1. **`nvcr.io/nvidia/vllm:26.01-py3`** as the base image (has CUDA forward compatibility for Blackwell)
2. **`pip install vllm==0.15.1`** — upgrades vLLM (also downgrades PyTorch from NVIDIA's 2.10.0a0 to standard 2.9.1)
3. **`pip uninstall flash-attn -y`** — removes flash_attn which has ABI mismatch after the PyTorch downgrade. Blackwell uses FlashInfer MLA, not flash-attn, so this is safe.
4. **`FLASHINFER_DISABLE_VERSION_CHECK=1`** — bypasses minor version mismatch between flashinfer-python 0.6.1 and NGC's flashinfer-cubin 0.6.0

**Images that fail on RunPod B200:**
- `vllm/vllm-openai:latest`, `v0.15.0`, `v0.15.1`, `v0.15.1-cu130`, `nightly`

See [vllm#33447](https://github.com/vllm-project/vllm/issues/33447).

## Files

| File | Purpose |
|------|---------|
| `pod.py` | Pod lifecycle management (launch, wait, status, stop, start, destroy, test, url) |
| `benchmark.py` | Performance benchmarking (sequential, streaming, concurrent) |
| `litellm_config.yaml` | LiteLLM proxy config pointing at RunPod vLLM |
| `.env.example` | Template for env vars |
| `.env` | Your config with secrets (gitignored) |
| `.pod_id` | Auto-created — tracks active pod ID (gitignored) |
