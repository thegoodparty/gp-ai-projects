from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).parent


@dataclass
class BenchmarkConfig:
    pod_id: str
    vllm_port: int = 8000
    vllm_api_key: str = ""
    model_name: str = "nvidia/Kimi-K2.5-NVFP4"

    @property
    def base_url(self) -> str:
        return f"https://{self.pod_id}-{self.vllm_port}.proxy.runpod.net/v1"

    @property
    def headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.vllm_api_key:
            h["Authorization"] = f"Bearer {self.vllm_api_key}"
        return h

    @classmethod
    def from_env(cls, env_path: Path | None = None) -> BenchmarkConfig:
        if env_path is None:
            env_path = SCRIPT_DIR / ".env"
        load_dotenv(env_path)

        pod_id_file = SCRIPT_DIR / ".pod_id"
        if not pod_id_file.exists():
            raise FileNotFoundError(
                f"No .pod_id file found at {pod_id_file}. Run 'uv run llm_deployment/pod.py launch' first."
            )

        pod_id = pod_id_file.read_text().strip()
        if not pod_id:
            raise FileNotFoundError(
                f".pod_id file at {pod_id_file} is empty. Run 'uv run llm_deployment/pod.py launch' first."
            )

        return cls(
            pod_id=pod_id,
            vllm_port=int(os.environ.get("VLLM_PORT", "8000")),
            vllm_api_key=os.environ.get("VLLM_API_KEY", ""),
            model_name=os.environ.get("MODEL_NAME", "nvidia/Kimi-K2.5-NVFP4"),
        )


PROMPTS = [
    {"name": "short", "max_tokens": 50, "messages": [{"role": "user", "content": "What is 2+2?"}]},
    {
        "name": "medium",
        "max_tokens": 500,
        "messages": [{"role": "user", "content": "Explain quicksort in Python with code."}],
    },
    {
        "name": "long",
        "max_tokens": 2000,
        "messages": [
            {
                "role": "user",
                "content": "Write a detailed guide on building a REST API with FastAPI, including authentication, database integration, error handling, and testing.",
            }
        ],
    },
    {
        "name": "reasoning",
        "max_tokens": 1000,
        "messages": [
            {
                "role": "user",
                "content": "A farmer has 17 sheep. All but 9 run away. How many sheep does the farmer have left? Think step by step.",
            }
        ],
    },
    {
        "name": "code",
        "max_tokens": 1000,
        "messages": [
            {
                "role": "user",
                "content": "Write a Python class that implements a thread-safe LRU cache with TTL expiration.",
            }
        ],
    },
    {
        "name": "creative",
        "max_tokens": 500,
        "messages": [{"role": "user", "content": "Write a haiku about each planet in the solar system."}],
    },
    {
        "name": "tool_use",
        "max_tokens": 200,
        "messages": [
            {"role": "system", "content": "You have access to a get_weather function."},
            {"role": "user", "content": "What's the weather in Tokyo?"},
        ],
    },
    {
        "name": "multilingual",
        "max_tokens": 300,
        "messages": [
            {
                "role": "user",
                "content": "Translate 'The quick brown fox jumps over the lazy dog' into Spanish, French, German, Japanese, and Korean.",
            }
        ],
    },
]


async def single_request(
    client: httpx.AsyncClient, prompt: dict, config: BenchmarkConfig, *, stream: bool = False
) -> dict:
    body = {
        "model": config.model_name,
        "messages": prompt["messages"],
        "max_tokens": prompt["max_tokens"],
        "stream": stream,
    }

    start = time.perf_counter()
    ttfb = None

    if stream:
        chunk_count = 0
        usage_tokens = None
        async with client.stream(
            "POST", f"{config.base_url}/chat/completions", json=body, headers=config.headers, timeout=300
        ) as resp:
            async for line in resp.aiter_lines():
                if ttfb is None and line.startswith("data: {"):
                    ttfb = time.perf_counter() - start
                if line.startswith("data: {"):
                    try:
                        chunk = json.loads(line[6:])
                        if "usage" in chunk:
                            usage_tokens = chunk["usage"].get("completion_tokens")
                        delta = chunk["choices"][0]["delta"]
                        if delta.get("content"):
                            chunk_count += 1
                        if delta.get("reasoning_content"):
                            chunk_count += 1
                    except (json.JSONDecodeError, KeyError, IndexError):
                        pass
        end = time.perf_counter()
        tokens = usage_tokens if usage_tokens is not None else chunk_count
        return {
            "name": prompt["name"],
            "tokens": tokens,
            "ttfb": ttfb or (end - start),
            "total_time": end - start,
            "tok_per_sec": tokens / (end - start) if (end - start) > 0 else 0,
        }
    else:
        resp = await client.post(f"{config.base_url}/chat/completions", json=body, headers=config.headers, timeout=300)
        resp.raise_for_status()
        end = time.perf_counter()
        data = resp.json()
        usage = data.get("usage", {})
        completion_tokens = usage.get("completion_tokens", 0)
        prompt_tokens = usage.get("prompt_tokens", 0)
        return {
            "name": prompt["name"],
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_time": end - start,
            "tok_per_sec": completion_tokens / (end - start) if (end - start) > 0 else 0,
        }


async def run_sequential(config: BenchmarkConfig, *, stream: bool = False) -> list[dict]:
    print("=" * 60)
    print(f"SEQUENTIAL BENCHMARK (stream={stream})")
    print("=" * 60)
    results = []
    async with httpx.AsyncClient() as client:
        for prompt in PROMPTS:
            print(f"  Running: {prompt['name']:<15} (max_tokens={prompt['max_tokens']})...", end="", flush=True)
            result = await single_request(client, prompt, config, stream=stream)
            results.append(result)
            if stream:
                print(
                    f"  {result['tokens']:>5} tok  {result['ttfb']:.2f}s TTFB  {result['total_time']:.2f}s total  {result['tok_per_sec']:.1f} tok/s"
                )
            else:
                print(
                    f"  {result['completion_tokens']:>5} tok  {result['total_time']:.2f}s  {result['tok_per_sec']:.1f} tok/s"
                )
    return results


async def run_concurrent(config: BenchmarkConfig, concurrency: int, prompt_idx: int = 2) -> list[dict]:
    print("=" * 60)
    print(f"CONCURRENT BENCHMARK: {concurrency} parallel requests")
    print(f"  Prompt: {PROMPTS[prompt_idx]['name']} (max_tokens={PROMPTS[prompt_idx]['max_tokens']})")
    print("=" * 60)

    async with httpx.AsyncClient() as client:
        start = time.perf_counter()
        tasks = [single_request(client, PROMPTS[prompt_idx], config, stream=False) for _ in range(concurrency)]
        results = await asyncio.gather(*tasks)
        wall_time = time.perf_counter() - start

    total_tokens = sum(r["completion_tokens"] for r in results)
    times = [r["total_time"] for r in results]
    tps_values = [r["tok_per_sec"] for r in results]

    print(f"  Wall time:          {wall_time:.2f}s")
    print(f"  Total tokens:       {total_tokens}")
    print(f"  Aggregate tok/s:    {total_tokens / wall_time:.1f}")
    print(f"  Per-request avg:    {statistics.mean(times):.2f}s  ({statistics.mean(tps_values):.1f} tok/s)")
    print(f"  Per-request p50:    {statistics.median(times):.2f}s")
    print(f"  Per-request min:    {min(times):.2f}s")
    print(f"  Per-request max:    {max(times):.2f}s")
    return list(results)


async def run_concurrent_mixed(config: BenchmarkConfig, concurrency: int) -> list[dict]:
    print("=" * 60)
    print(f"MIXED CONCURRENT BENCHMARK: {concurrency} parallel (varied prompts)")
    print("=" * 60)

    prompts = [PROMPTS[i % len(PROMPTS)] for i in range(concurrency)]
    async with httpx.AsyncClient() as client:
        start = time.perf_counter()
        tasks = [single_request(client, p, config, stream=False) for p in prompts]
        results = await asyncio.gather(*tasks)
        wall_time = time.perf_counter() - start

    total_tokens = sum(r["completion_tokens"] for r in results)
    print(f"  Wall time:          {wall_time:.2f}s")
    print(f"  Total tokens:       {total_tokens}")
    print(f"  Aggregate tok/s:    {total_tokens / wall_time:.1f}")
    for r in sorted(results, key=lambda x: x["name"]):
        print(
            f"    {r['name']:<15} {r['completion_tokens']:>5} tok  {r['total_time']:.2f}s  {r['tok_per_sec']:.1f} tok/s"
        )
    return list(results)


async def main():
    parser = argparse.ArgumentParser(description="Benchmark vLLM on RunPod")
    parser.add_argument("--quick", action="store_true", help="Quick test (sequential only)")
    parser.add_argument("--concurrency", type=int, nargs="+", default=[2, 4, 8], help="Concurrency levels to test")
    args = parser.parse_args()

    config = BenchmarkConfig.from_env()

    print(f"Model: {config.model_name}")
    print(f"Endpoint: {config.base_url}")
    print()

    all_results = {}

    seq_results = await run_sequential(config, stream=False)
    all_results["sequential"] = seq_results
    print()

    if not args.quick:
        stream_results = await run_sequential(config, stream=True)
        all_results["streaming"] = stream_results
        print()

        for c in args.concurrency:
            print()
            conc_results = await run_concurrent(config, c, prompt_idx=2)
            all_results[f"concurrent_{c}"] = conc_results
            print()

        print()
        mixed = await run_concurrent_mixed(config, len(PROMPTS))
        all_results["mixed"] = mixed

    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    if "sequential" in all_results:
        tps = [r["tok_per_sec"] for r in all_results["sequential"]]
        print(f"  Sequential avg tok/s:  {statistics.mean(tps):.1f}")
    if "streaming" in all_results:
        ttfbs = [r["ttfb"] for r in all_results["streaming"]]
        print(f"  Streaming avg TTFB:    {statistics.mean(ttfbs):.2f}s")
        tps = [r["tok_per_sec"] for r in all_results["streaming"]]
        print(f"  Streaming avg tok/s:   {statistics.mean(tps):.1f}")
    for c in args.concurrency:
        key = f"concurrent_{c}"
        if key in all_results:
            total_tok = sum(r["completion_tokens"] for r in all_results[key])
            max_time = max(r["total_time"] for r in all_results[key])
            print(f"  Concurrent x{c} agg tok/s: {total_tok / max_time:.1f}")


if __name__ == "__main__":
    asyncio.run(main())
