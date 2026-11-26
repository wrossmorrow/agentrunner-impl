"""Standalone verification script for the Modal RecursiveSI/flint endpoint."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smoke test Modal RecursiveSI/flint OpenAI-compatible endpoint."
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("FLINT_VLLM_URL", ""),
        help="Base Modal endpoint URL (or FLINT_VLLM_URL env var).",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("FLINT_API_KEY", ""),
        help="Bearer API key (or FLINT_API_KEY env var).",
    )
    parser.add_argument(
        "--model",
        default="RecursiveSI/flint",
        help="Served model name (default: RecursiveSI/flint).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="HTTP timeout in seconds to accommodate cold starts (default: 300).",
    )
    args = parser.parse_args()
    if not args.url:
        parser.error("--url or FLINT_VLLM_URL is required")
    if not args.api_key:
        parser.error("--api-key or FLINT_API_KEY is required")
    args.url = args.url.rstrip("/")
    return args


def _format_redirects(resp: httpx.Response) -> str:
    if not resp.history:
        return ""
    return f" redirects={[r.status_code for r in resp.history]}"


def main() -> int:
    args = parse_args()
    auth_headers = {"Authorization": f"Bearer {args.api_key}"}

    with httpx.Client(timeout=args.timeout, follow_redirects=True) as client:
        # 1. Auth rejection check
        t0 = time.monotonic()
        unauth_resp = client.get(f"{args.url}/v1/models")
        t_unauth = time.monotonic() - t0
        if unauth_resp.status_code not in (401, 403):
            print(
                f"[FAIL] Unauthenticated /v1/models returned {unauth_resp.status_code}, "
                "expected 401/403",
                file=sys.stderr,
            )
            return 1
        print(
            f"[PASS] Auth rejection check: HTTP {unauth_resp.status_code} "
            f"({t_unauth:.2f}s){_format_redirects(unauth_resp)}"
        )

        # 2. Cold-start /v1/models check
        t0 = time.monotonic()
        models_resp = client.get(f"{args.url}/v1/models", headers=auth_headers)
        t_models = time.monotonic() - t0
        if models_resp.status_code != 200:
            print(
                f"[FAIL] GET /v1/models returned {models_resp.status_code}: {models_resp.text}",
                file=sys.stderr,
            )
            return 1
        models_data = models_resp.json()
        model_ids = [item.get("id") for item in models_data.get("data", [])]
        if args.model not in model_ids:
            print(
                f"[FAIL] Model {args.model!r} not found in /v1/models response: {model_ids}",
                file=sys.stderr,
            )
            return 1
        print(
            f"[PASS] /v1/models returned {model_ids} "
            f"({t_models:.2f}s){_format_redirects(models_resp)}"
        )

        # 3. Tool-calling /v1/chat/completions check
        tool_payload = {
            "model": args.model,
            "temperature": 0.0,
            "max_tokens": 512,
            "tool_choice": "auto",
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "Read a file from the repository.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "path": {
                                    "type": "string",
                                    "description": "Relative path to the file.",
                                }
                            },
                            "required": ["path"],
                        },
                    },
                }
            ],
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a code reviewer. Always use the read_file tool "
                        "to inspect requested files."
                    ),
                },
                {
                    "role": "user",
                    "content": "Inspect main.go in the repository root using read_file.",
                },
            ],
        }

        t0 = time.monotonic()
        tool_resp = client.post(
            f"{args.url}/v1/chat/completions",
            headers=auth_headers,
            json=tool_payload,
        )
        t_tool = time.monotonic() - t0
        if tool_resp.status_code != 200:
            print(
                f"[FAIL] Tool-calling /v1/chat/completions returned "
                f"{tool_resp.status_code}: {tool_resp.text}",
                file=sys.stderr,
            )
            return 1
        tool_body = tool_resp.json()
        choice = (tool_body.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            print(
                f"[FAIL] Expected tool_calls in response, got message={json.dumps(message)}",
                file=sys.stderr,
            )
            return 1

        first_call = tool_calls[0].get("function") or {}
        if first_call.get("name") != "read_file":
            print(
                f"[FAIL] Expected tool call 'read_file', got {first_call!r}",
                file=sys.stderr,
            )
            return 1

        reasoning = message.get("reasoning_content") or message.get("reasoning")
        usage = tool_body.get("usage") or {}
        print(
            f"[PASS] Tool-calling completion ({t_tool:.2f}s): "
            f"tool={first_call.get('name')} args={first_call.get('arguments')} "
            f"reasoning_present={bool(reasoning)} usage={usage}"
        )

        # 4. Warm latency check
        warm_payload = {
            "model": args.model,
            "temperature": 0.0,
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
        }
        t0 = time.monotonic()
        warm_resp = client.post(
            f"{args.url}/v1/chat/completions",
            headers=auth_headers,
            json=warm_payload,
        )
        t_warm = time.monotonic() - t0
        if warm_resp.status_code != 200:
            print(
                f"[FAIL] Warm /v1/chat/completions returned "
                f"{warm_resp.status_code}: {warm_resp.text}",
                file=sys.stderr,
            )
            return 1
        warm_content = ((warm_resp.json().get("choices") or [{}])[0].get("message") or {}).get(
            "content"
        )
        print(f"[PASS] Warm completion ({t_warm:.2f}s): content={warm_content!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
