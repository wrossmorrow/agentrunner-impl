# agentreview

Scale-to-zero cloud deployment of [`agentrunner`](https://github.com/wrossmorrow/github-agent-runner) on **GKE Autopilot** (CPU-only) backed by **Recursive's `flint`** running serverless on **Modal** (scale-to-zero GPU), fronted by **Envoy Gateway + Agent Router (Envoy AI Gateway)** and synced via **Infisical**.

```text
GitHub Actions PR ──(outbound poll)──> GKE Autopilot (CPU-only, scale-to-zero runners)
                                         ├── arc-systems: ARC v2 controller + listener
                                         ├── agentrunner-system: agentrunner-controller (Helm OCI)
                                         ├── infisical-operator-system: Infisical Secrets Operator
                                         ├── ai-gateway: Envoy Gateway v1.10 + Agent Router v1.3 (ClusterIP)
                                         └── actions-runners: ephemeral ARC v2 pods (native sidecars)
                                                 │
                                                 ▼ HTTPS (180s timeout, 32Mi buffers)
                                       Modal Serverless GPU (RecursiveSI/flint on B200, 0 -> 1)
```

---

## Repository Layout

```text
.
├── PLAN.md
├── README.md
├── justfile                                    # Root module entrypoint (modal, terraform, platform, runners)
├── modal/
│   ├── justfile                                # sync, check, auth, secret, download, deploy, smoke-test, logs, stop
│   ├── pyproject.toml                          # uv project + ruff/mypy config
│   ├── requirements.txt
│   ├── flint_server.py                         # Configurable BF16/NVFP4 vLLM server on Modal
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
│   ├── agentrunner/values.yaml                 # agentrunner-controller values (ghcr.io/wrossmorrow/agentrunner-github-auth:0.0.2)
│   ├── infisical/
│   │   ├── values.yaml                         # infisical-helm-charts/secrets-operator values
│   │   └── manifests/
│   │       ├── auth.yaml                       # InfisicalConnection + InfisicalAuth (Universal Auth)
│   │       └── ai-gateway-secret.yaml          # InfisicalStaticSecret -> ai-gateway/flint-modal-api-key
│   └── agent-router/
│       ├── envoy-gateway-values.yaml           # Extension manager hook (:1063) + GatewayNamespace deploy mode
│       ├── ai-gateway-values.yaml              # Agent Router controller resources
│       └── manifests/
│           ├── namespace.yaml                  # ai-gateway namespace
│           ├── gateway.yaml                    # GatewayClass, EnvoyProxy (ClusterIP + JSON access logs), Gateway (:8080)
│           ├── flint-backend.yaml              # Backend, BackendTLSPolicy, AIServiceBackend, BackendSecurityPolicy
│           └── routes.yaml                     # AIGatewayRoute, BackendTrafficPolicy (180s + 32Mi), ClientTrafficPolicy (32Mi)
└── runners/
    ├── justfile                                # check, apply-secrets, apply-policy, apply-toolchains, apply-deployments, apply, status
    ├── namespace.yaml                          # actions-runners namespace
    ├── secrets.yaml                            # InfisicalStaticSecrets for ARC, review bot, and gateway token
    ├── policy/org-review-policy.yaml           # ConfigMap (00-base.md) with read-only review + JSON verdict schema
    ├── toolchains/go-1.26.yaml                 # AgenticRunnerToolchain (agentrunner-golang:0.0.2)
    └── deployments/go-reviewer.yaml            # AgenticRunnerDeployment -> agent-router.ai-gateway.svc.cluster.local:8080/v1
```

---

## Prerequisites

Install local tools:
- `just`, `uv`, `terraform` (`>= 1.9`), `helm`, `kubectl`
- `gcloud` + `gke-gcloud-auth-plugin`

Local ignored env files (never committed):
- **`modal/.env`**: `HF_TOKEN`, `FLINT_API_KEY`, `FLINT_URL`
- **`.env`** (repo root): `INFISICAL_CLIENT_ID`, `INFISICAL_CLIENT_SECRET`
- **`terraform/backend.hcl`** & **`terraform/terraform.tfvars`**: copied from `*.example`

### Infisical Secret Layout (`prod` environment)

Create a Machine Identity (Universal Auth, read-only `Viewer` role on your Infisical project) and populate these folders in `prod`:

| Infisical Path | Keys | Synced K8s Secret |
| :--- | :--- | :--- |
| `/ai-gateway` | `FLINT_API_KEY` | `ai-gateway/flint-modal-api-key` (templated as `apiKey`) |
| `/actions-runners/arc-github-auth` | `github_app_id`, `github_app_installation_id`, `github_app_private_key` | `actions-runners/arc-github-auth` |
| `/actions-runners/review-bot-app-key` | `private-key.pem` | `actions-runners/review-bot-app-key` |
| `/actions-runners/agent-gateway-token` | `token` | `actions-runners/agent-gateway-token` |

---

## Deployment Walkthrough

### 1. Phase 1: Serverless `flint` on Modal

```sh
just modal sync
just modal auth
just modal secret                 # pushes FLINT_API_KEY from modal/.env
just modal download bf16          # pre-loads RecursiveSI/flint onto flint-hf-cache Volume
just modal deploy bf16            # deploys B200 endpoint; add returned URL as FLINT_URL in modal/.env
just modal smoke-test             # verifies 401 rejection, /v1/models, tool calling, warm latency
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

To tail Agent Router telemetry (`github_repo`, `github_pr`, `github_run_id`, token counts, and TTFT/ITL) during a PR run:

```sh
kubectl logs -n ai-gateway -l gateway.networking.k8s.io/owning-gateway-name=agent-router -c envoy -f
```

*(If `kubectl` label selector differs, query the pod directly via `kubectl get pods -n ai-gateway`.)*

---

## Operational Notes & Gotchas

- **Modal `flint` BF16 requires Blackwell (`B200`)**: `vllm/vllm-openai:flint` requires `FLASHINFER` attention and `flashinfer_trtllm` BF16 decode MoE (`sm100`), which fails on `H200` (`sm90`). `flint_server.py` mounts `flint-flashinfer-cache` at `/cache/flashinfer` (via `FLASHINFER_WORKSPACE_BASE` / `FLASHINFER_CACHE_DIR` since `/root/.cache/flashinfer` is non-empty in the base image), runs with `HF_HUB_OFFLINE=1` + `TRANSFORMERS_OFFLINE=1`, enables `--safetensors-load-strategy prefetch`, and caps `max_containers=1` (`FLINT_MAX_CONTAINERS`).
- **GKE Autopilot & Gateway API CRDs**: Autopilot pre-manages Standard-channel Gateway API CRDs (`kube-addon-manager`) and rejects experimental routes (`enforce-gateway-standard-channel`). `platform/justfile` installs Envoy Gateway CRDs with `crds.gatewayAPI.enabled=false` via `helm template ... | kubectl apply --server-side` (avoiding Helm's 1 MiB release Secret limit) and installs `gateway-helm` with `--skip-crds`.
- **Terraform quota project**: `provider "google"` sets `user_project_override = true` and `billing_project = var.project_id` so `google_billing_budget` works under local user ADC (requires `cloudresourcemanager.googleapis.com` enabled on the project).
- **Envoy `ext_proc` buffers & timeouts**: Agent Router's external processor buffers request JSON bodies. Without `requestBuffer` (`32Mi`, `LimitOnly`) and `connection.bufferLimit` (`32Mi`) on both `BackendTrafficPolicy` and `ClientTrafficPolicy`, multi-turn agent prompts >32 KiB hit `413 Payload Too Large` or stall until `504`. Rule timeouts are set to `180s` (`request` and `backendRequest`) to cover Modal's ~85s cached cold start.
- **`agentrunner-controller` upgrades**: `platform/justfile` runs `helm show crds ... | kubectl apply --server-side` before `helm upgrade --install` so CRD schema additions (like `contextWindow` and `maxTokens`) are applied on upgrades.
