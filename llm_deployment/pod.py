from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).parent
POD_ID_FILE = SCRIPT_DIR / ".pod_id"


class PodError(Exception):
    pass


@dataclass
class PodConfig:
    runpod_api_key: str = ""
    hf_token: str = ""
    model_name: str = "Qwen/Qwen3-Coder-Next-FP8"
    gpu_type_id: str = "NVIDIA H200 NVL"
    gpu_count: int = 1
    volume_size_gb: int = 200
    vllm_port: int = 8000
    vllm_api_key: str = ""
    pod_id_file: Path = field(default_factory=lambda: POD_ID_FILE)

    @property
    def pod_name(self) -> str:
        return f"vllm-{self.model_name.split('/')[-1].lower()}"

    @property
    def graphql_url(self) -> str:
        return f"https://api.runpod.io/graphql?api_key={self.runpod_api_key}"

    @classmethod
    def from_env(cls, env_path: Path | None = None) -> PodConfig:
        if env_path is None:
            env_path = SCRIPT_DIR / ".env"
        load_dotenv(env_path)

        def safe_int(key: str, default: int) -> int:
            raw = os.environ.get(key, str(default))
            try:
                return int(raw)
            except ValueError as e:
                raise PodError(f"Invalid integer for {key}: {raw!r}") from e

        return cls(
            runpod_api_key=os.environ.get("RUNPOD_API_KEY", ""),
            hf_token=os.environ.get("HF_TOKEN", ""),
            model_name=os.environ.get("MODEL_NAME", "Qwen/Qwen3-Coder-Next-FP8"),
            gpu_type_id=os.environ.get("GPU_TYPE_ID", "NVIDIA H200 NVL"),
            gpu_count=safe_int("GPU_COUNT", 1),
            volume_size_gb=safe_int("VOLUME_SIZE_GB", 200),
            vllm_port=safe_int("VLLM_PORT", 8000),
            vllm_api_key=os.environ.get("VLLM_API_KEY", ""),
        )


def api(query: str, config: PodConfig, *, client: httpx.Client | None = None) -> dict:
    own_client = client is None
    if own_client:
        client = httpx.Client()
    try:
        response = client.post(config.graphql_url, json={"query": query}, timeout=30)
        if response.status_code != 200:
            raise PodError(f"API error ({response.status_code}): {response.text}")
        try:
            data = response.json()
        except json.JSONDecodeError as e:
            raise PodError(f"Non-JSON response: {response.text[:200]}") from e
        if "errors" in data:
            raise PodError(f"GraphQL errors: {json.dumps(data['errors'], indent=2)}")
        return data
    finally:
        if own_client:
            client.close()


def get_pod_id(config: PodConfig) -> str:
    if config.pod_id_file.exists():
        return config.pod_id_file.read_text().strip()
    return ""


def save_pod_id(pod_id: str, config: PodConfig) -> None:
    config.pod_id_file.write_text(pod_id)


def base_url(pod_id: str, config: PodConfig) -> str:
    return f"https://{pod_id}-{config.vllm_port}.proxy.runpod.net/v1"


def vllm_headers(config: PodConfig) -> dict:
    if config.vllm_api_key:
        return {"Authorization": f"Bearer {config.vllm_api_key}"}
    return {}


def cmd_launch(config: PodConfig, *, wait: bool = False, client: httpx.Client | None = None) -> None:
    if not config.runpod_api_key:
        raise PodError("RUNPOD_API_KEY not set. Add it to .env")

    existing = get_pod_id(config)
    if existing:
        raise PodError(f"Pod {existing} already tracked in .pod_id\nRun 'destroy' first, or 'start' to resume.")

    api_key_arg = f" --api-key {config.vllm_api_key}" if config.vllm_api_key else ""
    vllm_args = (
        f"--model {config.model_name} --host 0.0.0.0 --port {config.vllm_port} "
        f"--tensor-parallel-size {config.gpu_count} --max-model-len 131072 "
        f"--enable-auto-tool-choice --tool-call-parser kimi_k2 --reasoning-parser kimi_k2 "
        f"--trust-remote-code{api_key_arg}"
    )
    docker_args = (
        f"bash -c 'pip install vllm==0.15.1 && "
        f"pip uninstall flash-attn -y && "
        f"FLASHINFER_DISABLE_VERSION_CHECK=1 "
        f"python3 -m vllm.entrypoints.openai.api_server {vllm_args}'"
    )

    env_block = "[]"
    if config.hf_token:
        env_block = f'[{{key: "HF_TOKEN", value: "{config.hf_token}"}}]'

    query = f"""
    mutation {{
      podFindAndDeployOnDemand(
        input: {{
          name: "{config.pod_name}"
          imageName: "nvcr.io/nvidia/vllm:26.01-py3"
          cloudType: ALL
          gpuTypeId: "{config.gpu_type_id}"
          gpuCount: {config.gpu_count}
          volumeInGb: {config.volume_size_gb}
          containerDiskInGb: 20
          dockerArgs: "{docker_args}"
          ports: "{config.vllm_port}/http"
          volumeMountPath: "/root/.cache"
          env: {env_block}
          minDownload: 1000
          minUpload: 500
        }}
      ) {{
        id
        name
        desiredStatus
        imageName
        machine {{
          gpuDisplayName
        }}
      }}
    }}
    """

    print("Launching pod...")
    print(f"  Model:  {config.model_name}")
    print(f"  GPU:    {config.gpu_count} x {config.gpu_type_id}")
    print(f"  Volume: {config.volume_size_gb}GB")
    print()

    result = api(query, config, client=client)
    pod_data = result.get("data", {}).get("podFindAndDeployOnDemand")

    if not pod_data or not pod_data.get("id"):
        raise PodError(f"Failed to create pod:\n{json.dumps(result, indent=2)}")

    pod_id = pod_data["id"]
    save_pod_id(pod_id, config)
    print(f"Pod created: {pod_id}")
    print("Saved to .pod_id")
    print()
    print(f"API endpoint (once ready): {base_url(pod_id, config)}")
    print()

    if wait:
        cmd_wait(config)
    else:
        print("Model download + load takes 5-15 min on first boot.")
        print("Run 'uv run llm_deployment/pod.py wait' to poll until ready.")


def cmd_wait(
    config: PodConfig,
    *,
    client: httpx.Client | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    max_attempts: int = 180,
) -> None:
    pod_id = get_pod_id(config)
    if not pod_id:
        raise PodError("No pod tracked. Run 'launch' first.")

    url = f"{base_url(pod_id, config)}/models"
    print(f"Waiting for vLLM to be ready at: {url}")
    print("This can take 5-15 minutes (model download + load)...")
    print()

    own_client = client is None
    if own_client:
        client = httpx.Client()
    try:
        for attempt in range(1, max_attempts + 1):
            try:
                resp = client.get(url, headers=vllm_headers(config), timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    print()
                    print("vLLM is ready!")
                    print(json.dumps(data, indent=2))
                    print()
                    print(f"Endpoint: {base_url(pod_id, config)}")
                    return
            except (httpx.RequestError, httpx.TimeoutException, json.JSONDecodeError):
                pass

            print(f"\r  Attempt {attempt}/{max_attempts}...", end="", flush=True)
            sleep_fn(10)
    finally:
        if own_client:
            client.close()

    print()
    raise PodError(f"Timed out after {max_attempts * 10}s. Check status with 'status' command.")


def cmd_status(config: PodConfig, *, client: httpx.Client | None = None) -> None:
    if not config.runpod_api_key:
        raise PodError("RUNPOD_API_KEY not set.")

    pod_id = get_pod_id(config)
    if pod_id:
        print(f"Tracked pod: {pod_id}")
        print()

    query = "{ myself { pods { id name desiredStatus runtime { uptimeInSeconds } machine { gpuDisplayName } } } }"

    result = api(query, config, client=client)
    pods = result.get("data", {}).get("myself", {}).get("pods", [])

    if not pods:
        print("No pods found.")
        if pod_id:
            config.pod_id_file.unlink(missing_ok=True)
            print(f"Removed stale .pod_id (pod {pod_id} no longer exists).")
        return

    pod_ids = {p["id"] for p in pods}
    for pod in pods:
        runtime = pod.get("runtime") or {}
        uptime = runtime.get("uptimeInSeconds", 0)
        uptime_str = f"{uptime // 3600}h {(uptime % 3600) // 60}m" if uptime else "N/A"
        gpu = pod.get("machine", {}).get("gpuDisplayName", "N/A")
        print(f"  {pod['id']}  {pod['name']:<25} {pod['desiredStatus']:<10} {gpu:<20} uptime: {uptime_str}")

    if pod_id and pod_id not in pod_ids:
        config.pod_id_file.unlink(missing_ok=True)
        print(f"\nRemoved stale .pod_id (pod {pod_id} no longer exists).")


def cmd_stop(config: PodConfig, *, client: httpx.Client | None = None) -> None:
    pod_id = get_pod_id(config)
    if not pod_id:
        raise PodError("No pod tracked. Nothing to stop.")

    query = f'mutation {{ podStop(input: {{ podId: "{pod_id}" }}) {{ id desiredStatus }} }}'
    api(query, config, client=client)
    print(f"Stopped pod {pod_id} (volume preserved, billing paused).")
    print("Run 'start' to resume.")


def cmd_start(config: PodConfig, *, client: httpx.Client | None = None) -> None:
    pod_id = get_pod_id(config)
    if not pod_id:
        raise PodError("No pod tracked. Run 'launch' first.")

    query = (
        f'mutation {{ podResume(input: {{ podId: "{pod_id}", gpuCount: {config.gpu_count} }}) {{ id desiredStatus }} }}'
    )
    api(query, config, client=client)
    print(f"Started pod {pod_id}. Model cached on volume — startup is faster.")
    print()
    print("Run 'uv run llm_deployment/pod.py wait' to poll until the API is ready.")


def cmd_destroy(
    config: PodConfig,
    *,
    client: httpx.Client | None = None,
    confirm_fn: Callable[[str], str] = input,
) -> None:
    pod_id = get_pod_id(config)
    if not pod_id:
        raise PodError("No pod tracked. Nothing to destroy.")

    confirm = confirm_fn(f"This will TERMINATE pod {pod_id} and DELETE its volume. Are you sure? (y/N): ")
    if confirm.lower() != "y":
        print("Aborted.")
        return

    query = f'mutation {{ podTerminate(input: {{ podId: "{pod_id}" }}) }}'
    api(query, config, client=client)
    config.pod_id_file.unlink(missing_ok=True)
    print(f"Pod {pod_id} destroyed and .pod_id removed.")


def cmd_test(config: PodConfig, *, client: httpx.Client | None = None) -> None:
    pod_id = get_pod_id(config)
    if not pod_id:
        raise PodError("No pod tracked. Run 'launch' first.")

    url = base_url(pod_id, config)
    print(f"Testing {url} ...")
    print()

    own_client = client is None
    if own_client:
        client = httpx.Client()
    try:
        print("=== /v1/models ===")
        try:
            resp = client.get(f"{url}/models", headers=vllm_headers(config), timeout=10)
            print(json.dumps(resp.json(), indent=2))
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            print(f"Error: {e}")

        print()
        print("=== /v1/chat/completions ===")
        try:
            resp = client.post(
                f"{url}/chat/completions",
                headers=vllm_headers(config),
                json={
                    "model": config.model_name,
                    "messages": [{"role": "user", "content": "Write a Python fibonacci function"}],
                    "max_tokens": 200,
                },
                timeout=60,
            )
            print(json.dumps(resp.json(), indent=2))
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            print(f"Error: {e}")
    finally:
        if own_client:
            client.close()


def cmd_url(config: PodConfig) -> None:
    pod_id = get_pod_id(config)
    if not pod_id:
        raise PodError("No pod tracked.")
    print(base_url(pod_id, config))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage RunPod vLLM deployment",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Lifecycle:
  First time:   uv run llm_deployment/pod.py launch --wait
  Daily use:    uv run llm_deployment/pod.py start
                uv run llm_deployment/pod.py wait
  Done for day: uv run llm_deployment/pod.py stop
  Tear down:    uv run llm_deployment/pod.py destroy
        """,
    )

    sub = parser.add_subparsers(dest="command")

    launch_p = sub.add_parser("launch", help="Create H200 pod and start vLLM")
    launch_p.add_argument("--wait", action="store_true", help="Poll until vLLM is ready")

    sub.add_parser("wait", help="Poll until the vLLM API is responding")
    sub.add_parser("status", help="Show all pods")
    sub.add_parser("stop", help="Stop pod (pause billing, keep volume)")
    sub.add_parser("start", help="Resume a stopped pod")
    sub.add_parser("destroy", help="Terminate pod and delete volume")
    sub.add_parser("test", help="Hit /v1/models and /v1/chat/completions")
    sub.add_parser("url", help="Print the API base URL")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    try:
        config = PodConfig.from_env()
    except PodError as e:
        print(str(e))
        sys.exit(1)

    commands = {
        "launch": lambda: cmd_launch(config, wait=args.wait),
        "wait": lambda: cmd_wait(config),
        "status": lambda: cmd_status(config),
        "stop": lambda: cmd_stop(config),
        "start": lambda: cmd_start(config),
        "destroy": lambda: cmd_destroy(config),
        "test": lambda: cmd_test(config),
        "url": lambda: cmd_url(config),
    }

    try:
        commands[args.command]()
    except PodError as e:
        print(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
