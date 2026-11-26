"""Modal serverless inference app for thinkingmachines/Inkling-2-Mini."""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any

import modal

MINUTES = 60
VLLM_PORT = 8000
REPO_ID = "thinkingmachines/Inkling-2-Mini"
SERVED_MODEL_NAME = "thinkingmachines/Inkling-2-Mini"
DSPARK_SPEC_CONFIG = (
    '{"method":"dspark","num_speculative_tokens":7,"draft_sample_method":"probabilistic"}'
)

GPU = os.environ.get("INKLING_GPU", "").strip() or "B200"
APP_NAME = os.environ.get("INKLING_APP_NAME", "").strip() or "inkling-2-mini"
MAX_MODEL_LEN = int(os.environ.get("INKLING_MAX_MODEL_LEN", "131072"))
GPU_MEMORY_UTILIZATION = float(os.environ.get("INKLING_GPU_MEMORY_UTILIZATION", "0.85"))
SCALEDOWN_WINDOW = int(os.environ.get("INKLING_SCALEDOWN_WINDOW", "300"))
MAX_CONCURRENT = int(os.environ.get("INKLING_MAX_CONCURRENT", "2"))
MAX_CONTAINERS = int(os.environ.get("INKLING_MAX_CONTAINERS", "1"))
ENABLE_DSPARK = os.environ.get("INKLING_ENABLE_DSPARK", "1").strip().lower() in (
    "1",
    "true",
    "yes",
)
ENABLE_GPU_SNAPSHOT = os.environ.get("INKLING_ENABLE_GPU_SNAPSHOT", "0").strip().lower() in (
    "1",
    "true",
    "yes",
)
SECRET_NAME = os.environ.get("INKLING_SECRET_NAME", "inkling-api-key").strip() or "inkling-api-key"

hf_cache_vol = modal.Volume.from_name("inkling-hf-cache", create_if_missing=True)
vllm_cache_vol = modal.Volume.from_name("inkling-vllm-cache", create_if_missing=True)
flashinfer_cache_vol = modal.Volume.from_name("inkling-flashinfer-cache", create_if_missing=True)

vllm_env = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_HOME": "/root/.cache/huggingface",
    "VLLM_CACHE_ROOT": "/root/.cache/vllm",
    "FLASHINFER_WORKSPACE_BASE": "/cache/flashinfer",
    "FLASHINFER_CACHE_DIR": "/cache/flashinfer/.cache/flashinfer",
    "INKLING_MAX_MODEL_LEN": str(MAX_MODEL_LEN),
    "INKLING_GPU_MEMORY_UTILIZATION": str(GPU_MEMORY_UTILIZATION),
    "INKLING_ENABLE_DSPARK": "1" if ENABLE_DSPARK else "0",
    "INKLING_ENABLE_GPU_SNAPSHOT": "1" if ENABLE_GPU_SNAPSHOT else "0",
    "INKLING_SECRET_NAME": SECRET_NAME,
}
if ENABLE_GPU_SNAPSHOT:
    vllm_env.update(
        {
            "VLLM_SERVER_DEV_MODE": "1",
            "TORCHINDUCTOR_COMPILE_THREADS": "1",
        }
    )

vllm_image = (
    modal.Image.from_registry("vllm/vllm-openai:inkling-2-mini", add_python="3.12")
    .entrypoint([])
    .env(vllm_env)
)

downloader_image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("huggingface-hub>=0.30.0")
    .env(
        {
            "HF_XET_HIGH_PERFORMANCE": "1",
            "HF_HOME": "/root/.cache/huggingface",
        }
    )
)

_hf_token = os.environ.get("HF_TOKEN", "").strip()
_downloader_secrets = [modal.Secret.from_dict({"HF_TOKEN": _hf_token})] if _hf_token else []

app = modal.App(APP_NAME)


@app.function(
    image=downloader_image,
    volumes={"/root/.cache/huggingface": hf_cache_vol},
    secrets=_downloader_secrets,
    timeout=60 * MINUTES,
)
def download_weights() -> str:
    """Pre-download thinkingmachines/Inkling-2-Mini weights onto the persistent HF volume."""
    from huggingface_hub import snapshot_download

    local_path = snapshot_download(repo_id=REPO_ID)
    print(f"Downloaded {REPO_ID} -> {local_path}")
    hf_cache_vol.commit()
    return local_path


_cls_kwargs: dict[str, Any] = {
    "image": vllm_image,
    "gpu": GPU,
    "max_containers": MAX_CONTAINERS,
    "scaledown_window": SCALEDOWN_WINDOW,
    "timeout": 20 * MINUTES,
    "volumes": {
        "/root/.cache/huggingface": hf_cache_vol,
        "/root/.cache/vllm": vllm_cache_vol,
        "/cache/flashinfer": flashinfer_cache_vol,
    },
    "secrets": [modal.Secret.from_name(SECRET_NAME)],
}
if ENABLE_GPU_SNAPSHOT:
    _cls_kwargs["enable_memory_snapshot"] = True
    _cls_kwargs["experimental_options"] = {"enable_gpu_snapshot": True}


@app.cls(**_cls_kwargs)
@modal.concurrent(max_inputs=MAX_CONCURRENT)
class InklingServer:
    """OpenAI-compatible vLLM server for thinkingmachines/Inkling-2-Mini."""

    server_process: subprocess.Popen[bytes] | None = None

    def _get_api_key(self) -> str:
        api_key = os.environ.get("INKLING_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(f"INKLING_API_KEY is missing from Modal Secret {SECRET_NAME!r}")
        return api_key

    def _build_vllm_cmd(self, api_key: str) -> list[str]:
        cmd = [
            "vllm",
            "serve",
            REPO_ID,
            "--served-model-name",
            SERVED_MODEL_NAME,
            "--host",
            "0.0.0.0",
            "--port",
            str(VLLM_PORT),
            "--api-key",
            api_key,
            "--tensor-parallel-size",
            "1",
            "--max-model-len",
            str(MAX_MODEL_LEN),
            "--gpu-memory-utilization",
            str(GPU_MEMORY_UTILIZATION),
            "--safetensors-load-strategy",
            "prefetch",
            "--enable-auto-tool-choice",
            "--tool-call-parser",
            "inkling",
            "--reasoning-parser",
            "inkling",
        ]
        if ENABLE_DSPARK:
            cmd.extend(["--speculative-config", DSPARK_SPEC_CONFIG])
        if ENABLE_GPU_SNAPSHOT:
            cmd.append("--enable-sleep-mode")
        return cmd

    def _http_request(
        self,
        url: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> int:
        req_headers = dict(headers or {})
        data: bytes | None = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = int(resp.status)
            if not 200 <= status < 300:
                raise RuntimeError(f"{method} {url} returned unexpected status {status}")
            return status

    def _wait_for_health(self, timeout_seconds: int = 10 * MINUTES) -> None:
        base_url = f"http://127.0.0.1:{VLLM_PORT}"
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.server_process is not None:
                exit_code = self.server_process.poll()
                if exit_code is not None:
                    raise RuntimeError(f"vLLM server exited early with code {exit_code}")
            try:
                if self._http_request(f"{base_url}/health", timeout=2.0) == 200:
                    return
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(2)
        raise RuntimeError(f"vLLM server failed to become healthy within {timeout_seconds}s")

    def _start_vllm(self) -> str:
        api_key = self._get_api_key()
        cmd = self._build_vllm_cmd(api_key)
        self.server_process = subprocess.Popen(cmd)
        self._wait_for_health()
        flashinfer_cache_vol.commit()
        vllm_cache_vol.commit()
        return api_key

    def _warmup_and_sleep(self, api_key: str) -> None:
        base_url = f"http://127.0.0.1:{VLLM_PORT}"
        payload = {
            "model": SERVED_MODEL_NAME,
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": 8,
            "temperature": 0.0,
        }
        headers = {"Authorization": f"Bearer {api_key}"}
        for _ in range(2):
            self._http_request(
                f"{base_url}/v1/chat/completions",
                method="POST",
                payload=payload,
                headers=headers,
                timeout=120.0,
            )

        self._http_request(f"{base_url}/sleep?level=1", method="POST", timeout=60.0)

    def _wake_up(self) -> None:
        base_url = f"http://127.0.0.1:{VLLM_PORT}"
        self._http_request(f"{base_url}/wake_up", method="POST", timeout=60.0)
        self._wait_for_health(timeout_seconds=120)

    if ENABLE_GPU_SNAPSHOT:

        @modal.enter(snap=True)
        def start_and_snapshot(self) -> None:
            api_key = self._start_vllm()
            self._warmup_and_sleep(api_key)
            print("GPU snapshot capture: vLLM warmed up and put to sleep")

        @modal.enter(snap=False)
        def wake_up_server(self) -> None:
            self._wake_up()
            print("GPU snapshot restore: vLLM woken up and healthy")

    else:

        @modal.enter()
        def start_server(self) -> None:
            self._start_vllm()

    @modal.exit()
    def stop_server(self) -> None:
        if self.server_process is not None and self.server_process.poll() is None:
            self.server_process.terminate()
            try:
                self.server_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.server_process.kill()
                self.server_process.wait(timeout=5)

    @modal.web_server(port=VLLM_PORT, startup_timeout=10 * MINUTES)
    def serve(self) -> None:
        pass
