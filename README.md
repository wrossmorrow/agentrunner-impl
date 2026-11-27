# agentrunner implementation

Scale-to-zero cloud deployment of [`agentrunner`](https://github.com/wrossmorrow/github-agent-runner) on **GKE Autopilot** (CPU-only) backed by serverless **Modal** GPUs (`RecursiveSI/flint`, `thinkingmachines/Inkling-2-Mini`, `Qwen/Qwen3.8-27B`) and **GCP Vertex AI** (`^gemini-.*`), fronted by **Envoy Gateway + Agent Router (Envoy AI Gateway)** and synced via **Infisical**.

> See [docs/inference-and-scaling.md](docs/inference-and-scaling.md) for measured cold-start / GPU snapshot benchmarks, failover trade-offs, and a personal vs. 100-person engineering team cost & architecture comparison.

```text
GitHub Actions PR/Release ──(outbound poll)──> GKE Autopilot (CPU-only, scale-to-zero runners)
                                                 ├── arc-systems: ARC v2 controller + listener
                                                 ├── agentrunner-system: agentrunner-controller (Helm OCI)
                                                 ├── infisical-operator-system: Infisical Secrets Operator
                                                 ├── ai-gateway: Envoy Gateway v1.10 + Agent Router v1.3 (ClusterIP)
                                                 └── actions-runners: ephemeral ARC v2 pods (native sidecars)
                                                         │
                                                         ├──> HTTPS / HTTP/2 (180s timeout, 32Mi buffers, SecurityPolicy)
                                                         │      Modal Serverless GPUs (0 -> 1)
                                                         │        ├── B200: RecursiveSI/flint
                                                         │        ├── B200: thinkingmachines/Inkling-2-Mini (DSpark)
                                                         │        └── H100: Qwen/Qwen3.8-27B (FP8/BF16 + MTP)
                                                         │
                                                         └──> HTTPS / Workload Identity ADC (roles/aiplatform.user)
                                                                GCP Vertex AI (global: ^gemini-.*)
```

---

## Repository Layout

```text
.
├── .github/workflows/ci.yaml                   # Runs modal, terraform, and manifest check suites
├── PLAN.md
├── README.md
├── docs/
│   └── inference-and-scaling.md                # Cold-start data, failover trade-offs, personal vs. 100-person scale
├── justfile                                    # modal, terraform, platform, runners, bump-agentrunner, upgrade-agentrunner
├── modal/
│   ├── justfile                                # sync, check, auth, flint/inkling/qwen download/deploy/smoke-test/logs/stop
│   ├── pyproject.toml                          # uv project + ruff/mypy config
│   ├── requirements.txt
│   ├── flint_server.py                         # Configurable BF16/NVFP4 vLLM server on Modal (B200)
│   ├── inkling_server.py                       # ThinkingMachines Inkling-2-Mini + DSpark vLLM server on Modal (B200)
│   ├── qwen_server.py                          # Qwen/Qwen3.8-27B (FP8/BF16 + MTP) vLLM server on Modal (H100)
│   └── smoke_test.py                           # Auth, cold-start, tool-calling, and warm latency check
├── terraform/
│   ├── justfile                                # fmt, validate, check, init, plan, apply, destroy, kubeconfig
│   ├── backend.tf / backend.hcl.example        # GCS remote state
│   ├── variables.tf / terraform.tfvars.example # Cluster, CIDRs, optional authorized_networks & billing budget
│   ├── main.tf                                 # VPC, Cloud Router + Cloud NAT, GKE Autopilot, Vertex AI API
│   ├── iam.tf                                  # Least-privilege node SA, Agent Router Vertex IAM, billing budget
│   └── outputs.tf                              # Cluster info + gcloud get-credentials command
├── platform/
│   ├── justfile                                # check, infisical-*, arc-install, agentrunner-install, gateway-*, sync, status
│   ├── arc/values.yaml                         # ARC v2 gha-runner-scale-set-controller values
│   ├── agentrunner/values.yaml                 # agentrunner-controller values (ghcr.io/wrossmorrow/agentrunner-github-auth:0.0.9)
│   ├── infisical/
│   │   ├── values.yaml                         # infisical-helm-charts/secrets-operator values
│   │   └── manifests/
│   │       ├── auth.yaml                       # InfisicalConnection + InfisicalAuth (Universal Auth)
│   │       └── ai-gateway-secret.yaml          # InfisicalStaticSecrets -> {flint,inkling,qwen}-modal-api-key + agent-router-clients
│   └── agent-router/
│       ├── envoy-gateway-values.yaml           # Extension manager hook (:1063) + GatewayNamespace deploy mode
│       ├── ai-gateway-values.yaml              # Agent Router controller resources
│       └── manifests/
│           ├── namespace.yaml                  # ai-gateway namespace
│           ├── gateway.yaml                    # GatewayClass, EnvoyProxy (ClusterIP + JSON access logs), Gateway (:8080)
│           ├── flint-backend.yaml              # Backend, BackendTLSPolicy, AIServiceBackend, BackendSecurityPolicy (flint)
│           ├── inkling-backend.yaml            # Backend, BackendTLSPolicy, AIServiceBackend, BackendSecurityPolicy (Inkling-2-Mini)
│           ├── qwen-backend.yaml               # Backend, BackendTLSPolicy, AIServiceBackend, BackendSecurityPolicy (Qwen3.8-27B)
│           ├── vertex-backend.yaml             # Backend, BackendTLSPolicy, AIServiceBackend (GCPVertexAI), Workload Identity ADC
│           └── routes.yaml                     # AIGatewayRoute, BackendTrafficPolicy (HTTP/2 + retries), ClientTrafficPolicy, SecurityPolicy
└── runners/
    ├── justfile                                # check, apply-secrets, apply-policy, apply-toolchains, apply-deployments, apply, status
    ├── namespace.yaml                          # actions-runners namespace
    ├── secrets.yaml                            # InfisicalStaticSecrets for ARC, review/release bot apps, and per-repo gateway tokens
    ├── policy/
    │   ├── org-review-policy.yaml              # ConfigMap (00-base.md) with language-agnostic review + JSON verdict rules
    │   └── release-notes-policy.yaml           # ConfigMap (00-base.md) for read-only release-notes drafting
    ├── toolchains/go-1.26.yaml                 # AgenticRunnerToolchain (agentrunner-golang:0.0.9)
    └── deployments/
        ├── envoy-extproc-sdk-go.yaml           # PR reviewer for wrossmorrow/envoy-extproc-sdk-go
        ├── github-agent-runner.yaml            # PR reviewer for wrossmorrow/github-agent-runner
        └── github-agent-runner-release.yaml    # Release-notes drafter for wrossmorrow/github-agent-runner
```

---

## Prerequisites

Install local tools:
- `just`, `uv`, `terraform` (`>= 1.9`), `helm`, `kubectl`
- `gcloud` + `gke-gcloud-auth-plugin`

Local ignored env files (never committed):
- **`modal/.env`**: `HF_TOKEN`, `FLINT_API_KEY`, `FLINT_VLLM_URL`, `INKLING_API_KEY`, `INKLING_VLLM_URL`, `QWEN_API_KEY`, `QWEN_VLLM_URL`
- **`.env`** (repo root): `INFISICAL_CLIENT_ID`, `INFISICAL_CLIENT_SECRET`
- **`terraform/backend.hcl`** & **`terraform/terraform.tfvars`**: copied from `*.example`

### Infisical Secret Layout (`prod` environment)

Create a Machine Identity (Universal Auth, read-only `Viewer` role on your Infisical project) and populate these folders in `prod`:

| Infisical Path | Keys | Synced K8s Secret |
| :--- | :--- | :--- |
| `/ai-gateway` | `FLINT_API_KEY`, `INKLING_API_KEY`, `QWEN_API_KEY` | `ai-gateway/flint-modal-api-key`, `ai-gateway/inkling-modal-api-key`, `ai-gateway/qwen-modal-api-key` (each templated as `apiKey`) |
| `/actions-runners/arc-github-auth` | `github_app_id`, `github_app_installation_id`, `github_app_private_key` | `actions-runners/arc-github-auth` |
| `/actions-runners/review-bot-app-key` | `github_app_id`, `github_app_installation_id`, `private-key.pem` | `actions-runners/review-bot-app-key` |
| `/actions-runners/release-bot-app` | `github_app_id`, `github_app_installation_id`, `private-key.pem` | `actions-runners/release-bot-app` |
| `/actions-runners/agent-gateway-token` | `token`, `envoy_extproc_sdk_go`, `github_agent_runner` | `actions-runners/agent-gateway-token*` + `ai-gateway/agent-router-clients` (`go-reviewer`, `envoy-extproc-sdk-go`, `github-agent-runner`) |

---

## Deployment Walkthrough

### 1. Phase 1: Serverless Models on Modal

```sh
just modal sync
just modal auth

# RecursiveSI/flint (B200)
just modal secret                 # pushes FLINT_API_KEY from modal/.env
just modal download bf16          # pre-loads RecursiveSI/flint onto flint-hf-cache Volume
just modal deploy bf16            # add returned URL as FLINT_VLLM_URL in modal/.env
just modal smoke-test

# thinkingmachines/Inkling-2-Mini + DSpark (B200)
just modal inkling-secret         # pushes INKLING_API_KEY from modal/.env
just modal inkling-download       # pre-loads thinkingmachines/Inkling-2-Mini onto inkling-hf-cache Volume
just modal inkling-deploy B200 1  # add returned URL as INKLING_VLLM_URL in modal/.env
just modal inkling-smoke-test

# Qwen/Qwen3.8-27B + MTP (H100, fp8 default)
just modal qwen-secret            # pushes QWEN_API_KEY from modal/.env
just modal qwen-download          # pre-loads Qwen/Qwen3.8-27B-FP8 onto qwen-hf-cache Volume
just modal qwen-deploy fp8 H100 1 # add returned URL as QWEN_VLLM_URL in modal/.env
just modal qwen-smoke-test
```

Local code checks:
```sh
just modal check                  # ruff format --check, ruff check, mypy, py_compile, import checks
```

### 2. Phase 2: GKE Autopilot via Terraform

```sh
gcloud auth login
gcloud auth application-default login
gcloud config set project <PROJECT_ID>
gcloud auth application-default set-quota-project <PROJECT_ID>
gcloud services enable cloudresourcemanager.googleapis.com --project <PROJECT_ID>

cp terraform/backend.hcl.example terraform/backend.hcl
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
# edit terraform/backend.hcl and terraform/terraform.tfvars

just terraform check
just terraform init -backend-config=backend.hcl
just terraform plan
just terraform apply
just terraform kubeconfig
```

### 3. Phase 3: Platform Controllers & Agent Router

```sh
just platform check
just platform infisical-install
just platform infisical-auth
just platform infisical-apply
just platform sync                # installs ARC v2, agentrunner-controller, Envoy Gateway v1.10, Agent Router v1.3, routes
just platform status
```

### 4. Phase 4: Runner Toolchains, Deployments & Verification

```sh
just runners check
just runners apply
just runners status
kubectl get networkpolicy -n actions-runners -o yaml
```

To bump `agentrunner` chart + image versions (`platform/justfile`, `platform/agentrunner/values.yaml`, `runners/toolchains/*.yaml`, and `README.md`) and roll out an upgrade:

```sh
just bump-agentrunner 0.0.9       # update files and run platform/runners checks only
just upgrade-agentrunner 0.0.9    # bump, helm upgrade controller, apply toolchains, wait for rollout
```

To tail Agent Router telemetry (`client_id`, `github_repo`, `github_pr`, `github_run_id`, `upstream_protocol`, token counts, and TTFT/ITL) during a PR run:

```sh
kubectl get pods -n ai-gateway -l gateway.envoyproxy.io/owning-gateway-name=agent-router -o name | xargs -I{} kubectl logs -n ai-gateway {} -c envoy -f
```

---

## Operational Notes & Gotchas

- **Modal GPU tiers & vLLM constraints**:
  - `flint` and `Inkling-2-Mini` images require Blackwell (`B200` / `sm100`): `vllm/vllm-openai:flint` needs `FLASHINFER` + `flashinfer_trtllm` BF16 decode MoE (failing on `H200`), and `vllm/vllm-openai:inkling-2-mini`'s `conv_mix.cu` ships `sm100` cubins (failing on `H100`).
  - `Qwen/Qwen3.8-27B` runs on standard `vllm/vllm-openai` on `H100`, but uses hybrid Gated DeltaNet (Mamba) layers that allocate one state block per max sequence — `--max-num-seqs 16` prevents vLLM's `1024` default from exceeding available Mamba blocks (`719` on 80 GB `H100`).
  - All Modal apps mount persistent `/root/.cache/huggingface`, `/root/.cache/vllm`, and `/cache/flashinfer` volumes (via `FLASHINFER_WORKSPACE_BASE` / `FLASHINFER_CACHE_DIR` because `/root/.cache/flashinfer` is non-empty in the base images), enforce `HF_HUB_OFFLINE=1` + `TRANSFORMERS_OFFLINE=1`, enable `--safetensors-load-strategy prefetch`, bake deploy-time flags into `vllm_env` so `@modal.enter(snap=True)` sees them inside the container, and cap `max_containers=1`.
- **GKE Autopilot & Gateway API CRDs**: Autopilot pre-manages Standard-channel Gateway API CRDs (`kube-addon-manager`) and rejects experimental routes (`enforce-gateway-standard-channel`). `platform/justfile` installs Envoy Gateway CRDs with `crds.gatewayAPI.enabled=false` via `helm template ... | kubectl apply --server-side` (avoiding Helm's 1 MiB release Secret limit) and installs `gateway-helm` with `--skip-crds`.
- **Terraform quota project**: `provider "google"` sets `user_project_override = true` and `billing_project = var.project_id` so `google_billing_budget` works under local user ADC (requires `cloudresourcemanager.googleapis.com` enabled on the project).
- **Envoy `ext_proc`, HTTP/2, keepalives & inbound auth**:
  - `ext_proc` buffers request JSON bodies (`requestBuffer` `32Mi` `BufferAndLimit`, `connection.bufferLimit: 32Mi` on both `BackendTrafficPolicy` and `ClientTrafficPolicy`) to prevent `413` / `504` on large multi-turn prompts.
  - Route timeouts are `180s` with `connectionIdleTimeout: 15s`, upstream HTTP/2 window/PING keepalives (`initialStreamWindowSize: 2Mi`, `connectionKeepalive` every `15s`), and retries (`numRetries: 2` on `reset`, `connect-failure`, `502`, `503`) to avoid Modal edge / Cloud NAT stale-socket hangs.
  - Inbound `SecurityPolicy` (`apiKeyAuth`) automatically strips a leading `"Bearer "` prefix from `Authorization` before matching `ai-gateway/agent-router-clients` (store raw tokens in the Secret), sets `sanitize: true`, and emits the authenticated key name via `x-client-id` (`client_id`).
  - Vertex AI (`^gemini-.*`) authenticates via GKE Workload Identity ADC (`GCPCredentials` with `roles/aiplatform.user` bound to `ns/ai-gateway/sa/envoy-ai-gateway-agent-router-f09a7a01`), requiring zero static GCP keys.
- **`agentrunner-controller` upgrades**: `platform/justfile` runs `helm show crds ... | kubectl apply --server-side` before `helm upgrade --install` so CRD schema additions (`contextWindow`, `maxTokens`, `maxTransportRetries`, `mode`, `githubAppConfigMapRef`) are applied cleanly on upgrades.

### Cold-Start & Cost Observations (Modal GPU Snapshots)

- **GPU memory snapshots (`enable_gpu_snapshot`)**: Measured on `B200`, `Inkling-2-Mini` (`~24 GiB` weights) dropped from `~350s` uncached compile/init to **`13.7s`–`24s`** on snapshot restore (even across 13+ hours idle), whereas `flint` BF16 (`~76 GiB` weights, mostly storage I/O) is already `~85s` cold without snapshots and showed no speedup from best-effort snapshots.
- **Low-concurrency tuning**: Setting `--max-num-seqs 16` (`qwen_server.py`) or `8` avoids capturing dozens of large-batch CUDA graphs during cold boot.
- **Tighten `scaledown_window`**: All apps default to `scaledown_window=300` (5 min idle). Lowering `*_SCALEDOWN_WINDOW` to `60`–`120` cuts idle GPU cost when paired with fast-restoring snapshots.
- **Quantized presets**: `flint` supports `nvfp4` (`just modal download nvfp4 && just modal deploy nvfp4`) and `Qwen3.8-27B` defaults to `fp8` (`~28 GB` vs. `~54 GB` BF16) for faster loads and higher KV concurrency on `H100`.
