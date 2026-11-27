# agentrunner implementation

Scale-to-zero cloud deployment of [`agentrunner`](https://github.com/wrossmorrow/github-agent-runner) on **GKE Autopilot** (CPU-only) backed by **Recursive's `flint`** and **Thinking Machines' `Inkling-2-Mini`** running serverless on **Modal** (scale-to-zero GPU), fronted by **Envoy Gateway + Agent Router (Envoy AI Gateway)** and synced via **Infisical**.

> See [docs/inference-and-scaling.md](docs/inference-and-scaling.md) for measured cold-start / GPU snapshot benchmarks, failover trade-offs, and a personal vs. 100-person engineering team cost & architecture comparison.

```text
GitHub Actions PR ──(outbound poll)──> GKE Autopilot (CPU-only, scale-to-zero runners)
                                         ├── arc-systems: ARC v2 controller + listener
                                         ├── agentrunner-system: agentrunner-controller (Helm OCI)
                                         ├── infisical-operator-system: Infisical Secrets Operator
                                         ├── ai-gateway: Envoy Gateway v1.10 + Agent Router v1.3 (ClusterIP)
                                         └── actions-runners: ephemeral ARC v2 pods (native sidecars)
                                                 │
                                                 ▼ HTTPS (180s timeout, 32Mi buffers, SecurityPolicy)
                                       Modal Serverless GPU (B200, 0 -> 1)
                                         ├── RecursiveSI/flint
                                         └── thinkingmachines/Inkling-2-Mini (DSpark)
```

---

## Repository Layout

```text
.
├── PLAN.md
├── README.md
├── docs/
│   └── inference-and-scaling.md                # Cold-start data, failover trade-offs, personal vs. 100-person scale
├── justfile                                    # Root module entrypoint (modal, terraform, platform, runners)
├── modal/
│   ├── justfile                                # sync, check, auth, flint & inkling download/deploy/smoke-test/logs/stop
│   ├── pyproject.toml                          # uv project + ruff/mypy config
│   ├── requirements.txt
│   ├── flint_server.py                         # Configurable BF16/NVFP4 vLLM server on Modal (B200)
│   ├── inkling_server.py                       # ThinkingMachines Inkling-2-Mini + DSpark vLLM server on Modal (B200)
│   └── smoke_test.py                           # Auth, cold-start, tool-calling, and warm latency check
├── terraform/
│   ├── justfile                                # fmt, validate, check, init, plan, apply, destroy, kubeconfig
│   ├── backend.tf / backend.hcl.example        # GCS remote state
│   ├── variables.tf / terraform.tfvars.example # Cluster, CIDRs, optional authorized_networks & billing budget
│   ├── main.tf                                 # VPC, Cloud Router + Cloud NAT, GKE Autopilot (Gateway API + Dataplane V2)
│   ├── iam.tf                                  # Least-privilege node SA + optional GCP billing budget
│   └── outputs.tf                              # Cluster info + gcloud get-credentials command
├── platform/
│   ├── justfile                                # check, infisical-*, arc-install, agentrunner-install, gateway-*, sync, status
│   ├── arc/values.yaml                         # ARC v2 gha-runner-scale-set-controller values
│   ├── agentrunner/values.yaml                 # agentrunner-controller values (ghcr.io/wrossmorrow/agentrunner-github-auth:0.0.8)
│   ├── infisical/
│   │   ├── values.yaml                         # infisical-helm-charts/secrets-operator values
│   │   └── manifests/
│   │       ├── auth.yaml                       # InfisicalConnection + InfisicalAuth (Universal Auth)
│   │       └── ai-gateway-secret.yaml          # InfisicalStaticSecrets -> {flint,inkling}-modal-api-key + agent-router-clients
│   └── agent-router/
│       ├── envoy-gateway-values.yaml           # Extension manager hook (:1063) + GatewayNamespace deploy mode
│       ├── ai-gateway-values.yaml              # Agent Router controller resources
│       └── manifests/
│           ├── namespace.yaml                  # ai-gateway namespace
│           ├── gateway.yaml                    # GatewayClass, EnvoyProxy (ClusterIP + JSON access logs), Gateway (:8080)
│           ├── flint-backend.yaml              # Backend, BackendTLSPolicy, AIServiceBackend, BackendSecurityPolicy (flint)
│           ├── inkling-backend.yaml            # Backend, BackendTLSPolicy, AIServiceBackend, BackendSecurityPolicy (Inkling-2-Mini)
│           └── routes.yaml                     # AIGatewayRoute, BackendTrafficPolicy, ClientTrafficPolicy, SecurityPolicy
└── runners/
    ├── justfile                                # check, apply-secrets, apply-policy, apply-toolchains, apply-deployments, apply, status
    ├── namespace.yaml                          # actions-runners namespace
    ├── secrets.yaml                            # InfisicalStaticSecrets for ARC, review bot, and gateway token
    ├── policy/org-review-policy.yaml           # ConfigMap (00-base.md) with read-only review + JSON verdict schema
    ├── toolchains/go-1.26.yaml                 # AgenticRunnerToolchain (agentrunner-golang:0.0.8)
    └── deployments/go-reviewer.yaml            # AgenticRunnerDeployment -> agent-router.ai-gateway.svc.cluster.local:8080/v1
```

---

## Prerequisites

Install local tools:
- `just`, `uv`, `terraform` (`>= 1.9`), `helm`, `kubectl`
- `gcloud` + `gke-gcloud-auth-plugin`

Local ignored env files (never committed):
- **`modal/.env`**: `HF_TOKEN`, `FLINT_API_KEY`, `FLINT_VLLM_URL`, `INKLING_API_KEY`, `INKLING_VLLM_URL`
- **`.env`** (repo root): `INFISICAL_CLIENT_ID`, `INFISICAL_CLIENT_SECRET`
- **`terraform/backend.hcl`** & **`terraform/terraform.tfvars`**: copied from `*.example`

### Infisical Secret Layout (`prod` environment)

Create a Machine Identity (Universal Auth, read-only `Viewer` role on your Infisical project) and populate these folders in `prod`:

| Infisical Path | Keys | Synced K8s Secret |
| :--- | :--- | :--- |
| `/ai-gateway` | `FLINT_API_KEY`, `INKLING_API_KEY` | `ai-gateway/flint-modal-api-key`, `ai-gateway/inkling-modal-api-key` (each templated as `apiKey`) |
| `/actions-runners/arc-github-auth` | `github_app_id`, `github_app_installation_id`, `github_app_private_key` | `actions-runners/arc-github-auth` |
| `/actions-runners/review-bot-app-key` | `private-key.pem` | `actions-runners/review-bot-app-key` |
| `/actions-runners/agent-gateway-token` | `token` | `actions-runners/agent-gateway-token` + `ai-gateway/agent-router-clients` (`go-reviewer`) |

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
just modal inkling-deploy         # add returned URL as INKLING_VLLM_URL in modal/.env
just modal inkling-smoke-test
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

To tail Agent Router telemetry (`client_id`, `github_repo`, `github_pr`, `github_run_id`, token counts, and TTFT/ITL) during a PR run:

```sh
kubectl get pods -n ai-gateway -o name | head -n 1 | xargs kubectl logs -n ai-gateway -c envoy -f
```

---

## Operational Notes & Gotchas

- **Both `flint` and `Inkling-2-Mini` images require Blackwell (`B200`)**: `vllm/vllm-openai:flint` requires `FLASHINFER` attention and `flashinfer_trtllm` BF16 decode MoE (`sm100`, failing on `H200`), and `vllm/vllm-openai:inkling-2-mini`'s custom `conv_mix.cu` kernel ships `sm100` cubins (failing on `H100`). Both server apps mount persistent `/root/.cache/huggingface`, `/root/.cache/vllm`, and `/cache/flashinfer` volumes (via `FLASHINFER_WORKSPACE_BASE` / `FLASHINFER_CACHE_DIR` because `/root/.cache/flashinfer` is non-empty in the base images), enforce `HF_HUB_OFFLINE=1` + `TRANSFORMERS_OFFLINE=1`, enable `--safetensors-load-strategy prefetch`, and cap `max_containers=1`.
- **GKE Autopilot & Gateway API CRDs**: Autopilot pre-manages Standard-channel Gateway API CRDs (`kube-addon-manager`) and rejects experimental routes (`enforce-gateway-standard-channel`). `platform/justfile` installs Envoy Gateway CRDs with `crds.gatewayAPI.enabled=false` via `helm template ... | kubectl apply --server-side` (avoiding Helm's 1 MiB release Secret limit) and installs `gateway-helm` with `--skip-crds`.
- **Terraform quota project**: `provider "google"` sets `user_project_override = true` and `billing_project = var.project_id` so `google_billing_budget` works under local user ADC (requires `cloudresourcemanager.googleapis.com` enabled on the project).
- **Envoy `ext_proc` buffers, timeouts & inbound auth**: Agent Router's external processor buffers request JSON bodies and stamps `x-ai-eg-model` from the body's `model` field. Without `requestBuffer` (`32Mi`, `BufferAndLimit`) and `connection.bufferLimit` (`32Mi`) on both `BackendTrafficPolicy` and `ClientTrafficPolicy`, multi-turn agent prompts >32 KiB hit `413 Payload Too Large` or stall until `504`. Route timeouts are set to `180s` (`request` and `backendRequest`) with `connectionIdleTimeout: 30s` (avoiding Cloud NAT's idle socket black-holing). Inbound `SecurityPolicy` (`apiKeyAuth`) validates the runner's `Authorization: Bearer ...` header against `agent-router-clients`, strips it (`sanitize: true`), and logs the authenticated key name via `x-client-id` (`client_id`).
- **`agentrunner-controller` upgrades**: `platform/justfile` runs `helm show crds ... | kubectl apply --server-side` before `helm upgrade --install` so CRD schema additions (`contextWindow`, `maxTokens`) are applied on upgrades.

### Future Cold-Start & Cost Optimizations (Modal `flint` & `Inkling-2-Mini`)

- **Enable Modal GPU memory snapshots**: Deploy with `just modal deploy bf16 "" 1` (`FLINT_ENABLE_GPU_SNAPSHOT=1`) or `just modal inkling-deploy B200 1` (`INKLING_ENABLE_GPU_SNAPSHOT=1`) to checkpoint loaded weights and captured CUDA graphs in VRAM, cutting cold starts (especially `Inkling-2-Mini`'s `~224s` CUDA-graph + DSpark warmup) well below Agent Router's `180s` timeout and Modal's `150s` `303` redirect threshold.
- **Trim CUDA graph capture size for low concurrency**: With `MAX_CONCURRENT=2`, vLLM's default capture schedule still builds dozens of prefill/decode CUDA graphs up to batch size `512` (and `64` decode seqs for `Inkling-2-Mini`). Passing `--max-num-seqs 8` (or capping `max_cudagraph_capture_size`) cuts cold-start graph capture time without hurting single/two-PR latency.
- **Tighten `scaledown_window` once iteration settles**: Both apps default to `scaledown_window=300` (5 min idle). Lowering `FLINT_SCALEDOWN_WINDOW` / `INKLING_SCALEDOWN_WINDOW` to `60`–`120` reduces idle `B200` spend when paired with fast snapshot cold starts.
- **Smaller checkpoints / cheaper GPUs**: Benchmark `just modal download nvfp4 && just modal deploy nvfp4` (`RecursiveSI/flint-nvfp4-gguf`, ~20 GB vs. 74 GiB BF16) for faster 9P cold loads, or add a `llama-server` (`llama.cpp` CUDA) GGUF preset on `L40S` (48 GB) for ~3× lower \$/GPU-sec with zero FlashInfer JIT overhead.
