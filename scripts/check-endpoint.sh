#!/usr/bin/env bash
set -euo pipefail

MODEL="${1:-Qwen/Qwen3.8-27B}"
SECRET="${2:-agent-gateway-token}"
POD_NAME="gateway-probe-$$"

OVERRIDES="$(
  PROBE_MODEL="$MODEL" PROBE_SECRET="$SECRET" PROBE_POD="$POD_NAME" python3 - <<'PY'
import json
import os

body = json.dumps(
    {
        "model": os.environ["PROBE_MODEL"],
        "messages": [{"role": "user", "content": "Reply with OK."}],
        "max_tokens": 16,
    }
)

spec = {
    "spec": {
        "containers": [
            {
                "name": os.environ["PROBE_POD"],
                "image": "curlimages/curl:8.12.1",
                "env": [
                    {
                        "name": "AGENT_GATEWAY_TOKEN",
                        "valueFrom": {
                            "secretKeyRef": {
                                "name": os.environ["PROBE_SECRET"],
                                "key": "token",
                            }
                        },
                    },
                    {"name": "PROBE_BODY", "value": body},
                ],
                "command": [
                    "sh",
                    "-c",
                    'curl -sS -w "\\nHTTP %{http_code} (%{time_total}s)\\n" '
                    '-H "Authorization: Bearer $AGENT_GATEWAY_TOKEN" '
                    '-H "Content-Type: application/json" '
                    '-d "$PROBE_BODY" '
                    "http://agent-router.ai-gateway.svc.cluster.local:8080/v1/chat/completions",
                ],
            }
        ]
    }
}

print(json.dumps(spec))
PY
)"

exec kubectl run -n actions-runners --rm -i --restart=Never "$POD_NAME" \
  --image=curlimages/curl:8.12.1 \
  --overrides="$OVERRIDES"
