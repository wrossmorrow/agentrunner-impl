# Inference Backends, Cold Starts, and Scaling Economics

Notes on operating this architecture across two very different regimes:
1. **Personal development** (`~20–50 PRs/month`, bursty single-user cadence)
2. **100-person engineering organization** (`~200–400 PRs/day`, `~6,000 PRs/month`, sustained workday traffic with merge-rush spikes)

The main architectural takeaway is that the **control plane** deployed here (private GKE Autopilot + Cloud NAT, Infisical, ARC v2 ephemeral runners, `agentrunner-controller`, Envoy Gateway + Agent Router with `SecurityPolicy` and token-cost JSON telemetry) already has the production shape needed for a 100-person team. What changes between personal use and org scale is **how inference is provisioned and routed** behind Agent Router.

---

## 1. Abstracting Inference Behind Agent Router

Every downstream runner (`agentrunner` pods in `actions-runners`) sees one stable internal OpenAI-compatible interface:

- **Endpoint**: `http://agent-router.ai-gateway.svc.cluster.local:8080/v1` (`ClusterIP` only; no public ingress)
- **Auth**: Single internal bearer token validated at Envoy via `SecurityPolicy` (`apiKeyAuth`, sanitized before forwarding, mapped to `x-client-id` in JSON logs)
- **Telemetry**: Access logs capture `client_id`, `github_repo`, `github_pr`, `github_run_id`, `bytes_received`, `bytes_sent`, `response_flags`, and `llmRequestCosts`

Because `agentrunner`, `org-review-policy`, toolchains, and GitHub Actions workflows only speak OpenAI `/v1/chat/completions` to Agent Router, changing how models are hosted, tiered, or failed over happens purely inside `platform/agent-router/`:

- **Serverless GPUs** (Modal BF16/NVFP4 `vLLM` endpoints)
- **Self-hosted GKE GPU node pools** (dedicated or spot/preemptible GPUs inside the cluster VPC)
- **Managed cloud endpoints** (**GCP Vertex AI** via GKE Workload Identity, or **AWS Bedrock** via IAM role federation — keeping code diffs inside existing cloud trust boundaries with zero static API keys)

---

## 2. Measured Cold-Start & GPU Snapshot Findings (Modal `B200`)

Both `RecursiveSI/flint` and `thinkingmachines/Inkling-2-Mini` currently require Blackwell (`B200` / `sm100`) GPUs due to FlashInfer TRTLLM BF16 MoE (`flint`) and `conv_mix.cu` cubins (`Inkling-2-Mini`). With persistent `/root/.cache/huggingface`, `/root/.cache/vllm`, and `/cache/flashinfer` volumes, measured behavior from `0 -> 1` is:

| Model | Weight Footprint | Cached Cold Start (no snapshot) | GPU Snapshot Restore | Warm Latency | Operational Takeaway |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `RecursiveSI/flint` (`BF16`) | `~76 GiB` | `~85s` | Capture `101–153s` (best-effort cache missed across runs) | `0.3–0.7s` | Safe under Modal's `150s` `303` redirect threshold and Envoy's `180s` route timeout even **without** snapshots. |
| `thinkingmachines/Inkling-2-Mini` (+ `DSpark`) | `~24 GiB` | `~350s` (hits `303` redirects, exceeds `180s` timeout) | **`13.7s`** (`~25x` faster on hit; `~350–395s` cliff on miss) | `0.3–0.5s` | Huge win when snapshot hits, but snapshots are `experimental_options` (best-effort cache, cluster/eviction sensitive). |

**Why snapshots helped `Inkling-2-Mini` ~25x while helping `flint` much less:**
Modal stores GPU memory snapshots on the same distributed filesystem as Volumes. Workloads bottlenecked on raw weight reads (`flint` at `76 GiB`) see modest gains (and capture overhead on misses), whereas smaller models bottlenecked on Python imports, `torch.compile`, CUDA graph capture, and speculative-decoding initialization (`Inkling-2-Mini` at `24 GiB`) drop from minutes to ~14s when the snapshot hits.

---

## 3. Why "Fall Back to a Hosted API During Modal Warm-Up" Usually Backfires

A common idea for hiding serverless GPU cold starts is to configure Agent Router to route turn 1 to a hosted API (such as OpenRouter, Vertex AI, or Bedrock) while Modal spins up, then switch back to Modal once warm. In practice, both the gateway mechanics and the PR review lifecycle work against this:

### (a) What Agent Router can and cannot do alone
- Agent Router natively supports **prioritized failover** in `AIGatewayRoute` (`backendRefs` priority + `BackendTrafficPolicy` retries/timeouts) plus native schema translation and **GCP Vertex AI / AWS Bedrock** backends.
- However, Agent Router **cannot** inspect Modal container warmth out-of-band or fire a non-blocking wake-up ping while routing turn 1 elsewhere. The only way Envoy alone can wake Modal is to send the real request and abort it after a short `perTryTimeout`:
  - Too short (e.g., `5–10s`): normal prefill or deep-reasoning turns falsely fail over even when Modal is warm.
  - Too long (e.g., `30s+`): every cold turn eats that timeout before even starting the fallback request.

### (b) PR lifecycle & prefix-cache economics
- **You pay for a GPU you never use**: A typical 7–10 turn PR review on a warm cloud API completes in **`~15–45 seconds`**. By the time a cold `flint` (`~85s`) or missed-snapshot `Inkling` (`~350s`) container finishes booting, **the entire PR review is already over** — so Modal boots the `B200` and sits idle for the full `scaledown_window` (`300s`), billing for zero served turns.
- **Mid-session switching breaks KV/prefix caching**: Multi-turn agentic reviews reuse a large shared prefix (system prompt + org policy + file contents + prior tool calls). Switching providers mid-trajectory invalidates KV prefix caches and mixes two different models' reasoning and tool-calling formats in the same message history.
- **Trust boundaries**: Routing source diffs to third-party aggregator endpoints like OpenRouter expands the security boundary unnecessarily when Agent Router can speak directly to **Vertex AI** (GKE Workload Identity) or **Bedrock**.

### Better ways to handle cold starts if keeping serverless GPUs
1. **Use managed cloud endpoints for low-volume personal traffic**: Route directly to Vertex AI or Bedrock in `AIGatewayRoute` when volume is only tens of PRs/month.
2. **Overlap warm-up with runner setup**: If using fast-restoring snapshots like `Inkling-2-Mini` (`~14s`), send a tiny non-blocking warm-up ping at the start of the GitHub Actions job while ARC/GKE spins up the pod and checks out the repo — the container is warm before turn 1 begins.
3. **Or simply wait asynchronously**: GitHub PR reviews run in the background; an occasional `14–85s` cold start is often acceptable when requests are infrequent.

---

## 4. Personal Dev vs. 100-Person Engineering Team

| Dimension | Personal Dev (`~20–50 PRs/mo`) | 100-Person Eng Team (`~200–400 PRs/day`, `~6,000 PRs/mo`) |
| :--- | :--- | :--- |
| **Traffic shape** | Isolated single PRs separated by hours or days. | `20–40 PRs/hr` across `~10` work hours/day (`1` PR every `1.5–3 min`), quiet nights/weekends. |
| **Serverless GPU warmth** | Almost every PR hits a cold start and pays a full `scaledown_window` tail (`5 min` idle per PR). | A single GPU stays naturally warm during work hours (`scaledown_window=300` bridges gaps) and still scales to zero overnight/weekends (`~220–350 GPU-hrs/mo`). |
| **Modal `B200` (`~$6.25/hr`) inference cost** | `~6–7 min/PR` (`~$0.60–$0.75/PR`) -> **`~$15–$35/mo`** (`~$5–$10/mo` with `14s` snapshot + `60s` scaledown). | `~250–350 GPU-hrs/mo` with continuous batching + `max_containers=2–4` for bursts -> **`~$1,500–$2,200/mo`**. |
| **Vertex AI / Bedrock API inference cost** | Mostly-cached multi-turn reviews (`~$0.02–$0.10/PR`) -> **`~$1–$5/mo`**, zero cold starts. | Larger diffs + deeper trajectories (`~$0.15–$0.40/PR`) -> **`~$900–$2,400/mo`**, instant burst concurrency. |
| **Fixed GKE platform baseline** | **Dominates total bill**: Cloud NAT (`~$32/mo` + data) plus always-on controller pods (`arc`, `agentrunner`, `infisical`, Envoy/Agent Router). | **Negligible overhead** (`~1–3%` of total spend) across 6,000 PRs/month. |

*(Order-of-magnitude estimates assuming ~15k–40k mostly prefix-cached prompt tokens and ~2k–4k completion tokens per personal PR, and larger multi-file enterprise diffs at team scale.)*

---

## 5. Where Agent Router Actually Earns Its Keep at Org Scale

Once volume reaches a 100-person engineering team, Agent Router solves four real operational problems without changing any runner code:

1. **Overflow and HA failover (not cold-start bridging)**: Route Priority `0` to your warm self-hosted vLLM pool (Modal or GKE GPUs) and Priority `1` to Vertex AI / Bedrock so Friday merge-rush saturation (`429`/`503`) or a GPU node outage degrades seamlessly instead of failing CI checks.
2. **Per-repo and per-team budgets & attribution**: Use the `x-client-id`, `x-github-repo`, `x-github-run-id`, and `llmRequestCosts` metadata already logged by Agent Router to enforce token rate limits and produce chargeback/showback reports.
3. **Model tiering by repo or risk**: Route routine PRs to a fast, low-cost model (`Inkling-2-Mini`, Gemini Flash, Claude Haiku) and route security-sensitive repos or explicit deep-review labels to a stronger reasoning model (`flint`, Gemini Pro, Claude Sonnet).
4. **Zero-key cloud IAM upstream auth**: Replace static API keys with GKE Workload Identity (Vertex AI) or IRSA (Bedrock) inside the `ai-gateway` namespace while keeping one simple OpenAI bearer token for ephemeral runners.
