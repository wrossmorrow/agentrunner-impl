# Agentic Runner Cloud Deployment Plan (GKE Autopilot + Agent Router + Modal)

## Goal
Stand up a personal, scale-to-zero cloud deployment of [`agentrunner`](https://github.com/wrossmorrow/github-agent-runner) on **GKE Autopilot** (CPU-only) backed by **Recursive's `flint`** running serverless on **Modal** (scale-to-zero GPU), fronted by an in-cluster **Agent Router / AI Gateway**.

## Decisions
- **AI Gateway / Agent Router**: **Envoy AI Gateway** (Kubernetes Gateway API native, direct path to `InferencePool` later).
- **`flint` Serving (Modal)**: Configurable via env vars in `modal/flint_server.py` for **BF16** (`RecursiveSI/flint`, default `B200`) and **NVFP4** (`RecursiveSI/flint-nvfp4-gguf`, default `B200`) — `vllm/vllm-openai:flint` requires Blackwell (`FlashInfer TRTLLM`) — with optional GPU memory snapshot support.
- **Secrets Management**: **Infisical** (Infisical Kubernetes Secrets Operator for GKE Autopilot + Infisical CLI/sync for Modal).

```text
GitHub Actions PR ──(outbound poll)──> GKE Autopilot (CPU-only, scale-to-zero runners)
                                         ├── arc-systems: ARC v2 controller + listener
                                         ├── agentrunner-system: agentrunner-controller (Helm OCI)
                                         ├── ai-gateway: Agent Router (routing, auth, attribution, timeouts)
                                         └── actions-runners: ephemeral ARC v2 pods (native sidecars)
                                                 │
                                                 ▼ HTTPS (via Agent Router)
                                       Modal Serverless GPU (flint, scale-to-zero)
```

---

## Proposed Repository Layout
```text
.
├── PLAN.md
├── justfile                        # tf-apply, helm-sync, modal-deploy, bootstrap-secrets, smoke-test
├── terraform/
│   ├── main.tf                     # VPC, subnet, Cloud Router + Cloud NAT, GKE Autopilot cluster
│   ├── iam.tf                      # Workload Identity / Secret Manager / optional GCS bucket
│   ├── variables.tf
│   ├── outputs.tf                  # cluster name, region, get-credentials command
│   └── backend.tf                  # GCS remote state (bootstrap bucket)
├── modal/
│   ├── flint_server.py             # Modal app serving RecursiveSI/flint (/v1/chat/completions)
│   └── requirements.txt
├── platform/
│   ├── arc/values.yaml             # ARC v2 gha-runner-scale-set-controller Helm values
│   ├── agentrunner/values.yaml     # agentrunner-controller Helm OCI values
│   └── agent-router/               # Agent Router / AI Gateway Helm values + route config
└── runners/
    ├── toolchains/
    │   └── go-1.26.yaml            # AgenticRunnerToolchain
    └── deployments/
        └── go-reviewer.yaml        # AgenticRunnerDeployment -> points at in-cluster Agent Router
```

---

## Phase 1: Serverless `flint` on Modal (`modal/`)
1. **Model weights on a Modal Volume**: Pre-download `RecursiveSI/flint` weights onto a persistent `modal.Volume` so cold starts don't re-fetch weights from Hugging Face.
2. **OpenAI-compatible HTTP endpoint**: Serve `/v1/chat/completions` and `/v1/models` (using `vLLM` or `flint`'s native serving stack) behind a Modal web endpoint with:
   - Bearer token auth loaded from a `modal.Secret` (`FLINT_API_KEY`).
   - `@modal.concurrent(max_inputs=2)` (tune to VRAM/KV cache) + `scaledown_window=300` (scale to zero after 5 min idle).
   - Optional Modal GPU memory snapshots to cut cold-start latency.
3. **Verify standalone**: `curl` `/v1/models` and a tool-calling `/v1/chat/completions` request from cold start and measure cold vs. warm latency.

### Quantized & Cold-Start Notes
- **`RecursiveSI/flint-nvfp4-gguf` on Modal (`B200`)**: ~4× smaller weights (~20 GB vs. 74 GiB BF16) for faster 9P cold-start loading and native Blackwell FP4; deploy via `just modal download nvfp4 && just modal deploy nvfp4` (passes `--hf-config-path RecursiveSI/flint --tokenizer RecursiveSI/flint`).
- **GGUF quants via `llama-server` (`llama.cpp` CUDA) on cheaper GPUs (`L40S` 48 GB)**: Avoids `vllm/vllm-openai:flint`'s Blackwell-only `FlashInfer TRTLLM` requirement and FlashInfer JIT compile (~3× lower \$/GPU-sec than `B200`, fast cold starts), while keeping an OpenAI-compatible `/v1/chat/completions` + `--jinja` tool-calling API.
- **Cold-start findings (BF16 on `B200`)**: `vllm/vllm-openai:flint` requires Blackwell (`B200` / `sm100`) for `FlashInfer TRTLLM` MoE; mounting `flint-flashinfer-cache` at `/cache/flashinfer` (via `FLASHINFER_WORKSPACE_BASE` / `FLASHINFER_CACHE_DIR`) + `--safetensors-load-strategy=prefetch` caches the ~2m15s JIT compile, and GPU memory snapshots (`FLINT_ENABLE_GPU_SNAPSHOT=1`) can keep cold starts safely below Modal's ~150s `303` redirect threshold.
- **Alternative model note**: `thinkingmachines/Inkling-2-Mini` (26B MoE, 1M context, NVFP4/MXFP8 + DSpark speculative decoding) is a strong future candidate once the cluster is live—behind Envoy AI Gateway it's just adding/swapping an upstream backend.

---

## Phase 2: GKE Autopilot via Terraform (`terraform/`)
1. **Network**: Dedicated VPC + subnet + **Cloud Router & Cloud NAT** (so private Autopilot nodes have reliable outbound egress to GitHub, GHCR, and Modal without public node IPs).
2. **GKE Autopilot cluster**:
   - Kubernetes **1.29+** (native sidecars) with **Dataplane V2** (enforces `NetworkPolicy` automatically).
   - Workload Identity enabled.
   - Regional or single-region Autopilot (zero node-pool management; pay only for pod requests).
3. **Budget & IAM**: GCP billing budget alert + dedicated service accounts as needed.

---

## Phase 3: Platform Charts (`platform/`)
Install via Helm (or Terraform `helm_release` / `just` recipes):
1. **ARC v2 controller** (`arc-systems`):
   - `oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set-controller`
2. **`agentrunner-controller`** (`agentrunner-system`):
   - `oci://ghcr.io/wrossmorrow/charts/agentrunner-controller`
   - Configured with `controller.githubAuthImage=ghcr.io/wrossmorrow/agentrunner-github-auth:<ver>` (auto-detects ARC version).
3. **Agent Router / AI Gateway** (`ai-gateway`):
   - Expose an in-cluster Service (e.g., `http://agent-router.ai-gateway.svc.cluster.local:8080/v1`).
   - Configure Modal `flint` as an upstream HTTPS backend (holding the Modal bearer token in the gateway, optionally adding a cloud model like Claude/Gemini as a second route/fallback).
   - Set upstream response/idle timeouts (`>= 180s`) so Modal `0 → 1` cold starts don't time out Turn 1.
   - Log/export `x-github-repo`, `x-github-run-id`, and `x-github-pr` headers (already stamped by `agentrunner`) for per-PR cost/latency visibility.
   - *(Future)*: When/if self-hosted GPU pods are added on GKE, attach a Gateway API `InferencePool` behind this same router for prefix-cache-aware routing.

---

## Phase 4: Secrets, CRs & Live Verification (`runners/`)
1. **Secrets** (never committed to git; managed via **Infisical** — `InfisicalSecret` CRs via the Infisical Kubernetes Secrets Operator for GKE Autopilot, plus Infisical CLI/sync for Modal):
   - `actions-runners/arc-github-auth` (ARC GitHub App: `Administration: Read & write` for repo, or `Self-hosted runners: Read & write` for org)
   - `actions-runners/review-bot-app-key` (dedicated review-bot App: `Contents: Read-only`, `Pull requests: Read & write`)
   - `actions-runners/agent-gateway-token` (token accepted by the in-cluster Agent Router)
   - `actions-runners/org-review-policy` (`ConfigMap` with `00-base.md`)
2. **Apply CRs**:
   - `AgenticRunnerToolchain` (`go-1.26` → `ghcr.io/wrossmorrow/agentrunner-golang:v0.0.1`)
   - `AgenticRunnerDeployment` (`spec.agent.gateway.endpoint` → in-cluster Agent Router URL)
3. **End-to-end test**:
   - Add `concurrency: { group: agent-review-${{ github.event.pull_request.number }}, cancel-in-progress: true }` to the test repo workflow.
   - Open a PR while Modal is scaled to `0`, verify cold-start turn completes cleanly, review posts under the review-bot App, and ephemeral runner + Modal container both scale back to `0` afterward.

---

## Real-World Gotchas to Watch on GKE Autopilot ("What's Easy to Miss")
1. **Dataplane V2 enforces `NetworkPolicy`** (unlike `kind`'s `kindnet`): Verify `agentrunner`'s generated `NetworkPolicy` allows egress from `actions-runners` to the `ai-gateway` namespace/port (and DNS + 443 for GitHub/Modal).
2. **Autopilot security & resource constraints**:
   - Autopilot enforces CPU:memory ratios (typically 1:1 to 1:6.5 GiB per vCPU) and sets default requests on every sidecar (`egress-proxy`, `github-auth`, `agent`, `runner`)—size `AgenticRunnerToolchain.spec.resources` accordingly.
   - Verify `ubuntu/squid:latest` runs cleanly under Autopilot's admission policy (if Autopilot rejects root `squid` dropping privileges, swap to a non-root Squid config/image or lightweight Go forward proxy).
3. **Timeout chain alignment**: Ensure `Modal cold start (~30–45s)` < `Agent Router timeout (180s)` < `agentrunnerd maxDuration (10m)` < `GitHub Actions job timeout (15m)`.
4. **Cost guardrails**: Set Modal spend limits, GCP billing alerts, `maxRunners` on `AgenticRunnerDeployment`, and `maxTurns` / `maxDuration` on `spec.agent.limits`.
5. **Observability**: GKE Cloud Logging automatically ingests `egress-proxy` Squid audit logs, `agentrunner-controller` logs, and Agent Router access logs without leaking them into GitHub Actions stdout.