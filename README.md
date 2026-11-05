# rhapsode

Fills your [Homer](https://github.com/bastienwirtz/homer) dashboard with every public Kubernetes Ingress and Gateway API HTTPRoute. A Claude Agent SDK agent works out what each URL powers before it is listed.

Design, trade-offs and security model: [docs/design.md](docs/design.md).

```mermaid
flowchart LR
  K[(k8s API)] -->|Ingress, HTTPRoute| D[discovery]
  D --> C{cached?}
  C -- no --> A[agent run]
  A <-->|tools| T[get_workload / get_argocd_app / get_repo / probe_url]
  T <--> K
  T <--> G[GitHub / GitLab]
  T <--> U[the URL itself]
  C -- yes --> R[render config.yml]
  A --> R
  R -->|ConfigMap homer-config| H[Homer]
  A -->|label| CA[(ConfigMap rhapsode-cache)]
```

## How it works

- Every `RECONCILE_INTERVAL_S` (default 60) it lists Ingresses and HTTPRoutes.
- An endpoint gets one short agent run only when its host, backend, namespace or annotations change. Labels are cached in a ConfigMap, so in steady state there are no LLM calls. A failed run gets a fallback label and is retried after `FAILURE_BACKOFF_S` (default 30 min).
- The agent has no built-in tools: no shell, no files, no web. It has four tools of its own:
  - `get_workload`: backend Service and pod info.
  - `get_argocd_app`: the Argo CD Application.
  - `get_repo`: a GitHub or GitLab repo's description plus its README or one file.
  - `probe_url`: a GET on the URL. It only works on discovered hosts and does not follow redirects.
- Output is a structured JSON label. It is still validated: lengths are clamped, and `icon` and `category` must come from an allowlist. The URL never comes from the model.
- The result goes to ConfigMap `homer-config` (`config.yml`), built on top of `/config/base.yml`. Mount it in Homer as a directory at `/www/assets/rhapsode/`, plus a static `config.yml` containing `externalConfig: assets/rhapsode/config.yml`. Directory mounts update live. `subPath` mounts do not. The chart's install notes repeat this.

## "Public" means

The host is a real, non-wildcard hostname. Its namespace is not in `EXCLUDE_NAMESPACES`. It does not end in one of `EXCLUDE_HOST_SUFFIXES`. If `PUBLIC_INGRESS_CLASSES` is set (e.g. `cloudflare-tunnel`), its ingress class is in that list. Override per resource with the annotation `rhapsode/ignore: "true"|"false"`.

## LLM auth

| Mode | Env |
|------|-----|
| Anthropic API key | `ANTHROPIC_API_KEY` |
| OpenRouter | `ANTHROPIC_BASE_URL=https://openrouter.ai/api`, `ANTHROPIC_AUTH_TOKEN=<key>`, `ANTHROPIC_API_KEY=""`, `AGENT_MODEL=anthropic/claude-haiku-5.5` |
| Bedrock | `CLAUDE_CODE_USE_BEDROCK=1`, `AWS_REGION`, AWS credentials, `AGENT_MODEL=<inference profile id>` |

## Run

```bash
pip install -e '.[test]'
pytest -q tests

# local dry run against your kubeconfig
OUTPUT_PATH=./config.yml ANTHROPIC_API_KEY=... rhapsode --once
```

### Helm

```bash
kubectl -n homer create secret generic rhapsode --from-literal=openrouter-api-key=sk-or-...
helm install rhapsode oci://ghcr.io/barakplasma/charts/rhapsode --version <version> -n homer -f my-values.yaml
```

```yaml
# my-values.yaml (OpenRouter)
config:
  PUBLIC_INGRESS_CLASSES: cloudflare-tunnel
  FORCE_HTTPS: "true"
  AGENT_MODEL: anthropic/claude-haiku-5.5
extraEnv:
  - {name: ANTHROPIC_BASE_URL, value: https://openrouter.ai/api}
  - {name: ANTHROPIC_API_KEY, value: ""}
  - name: ANTHROPIC_AUTH_TOKEN
    valueFrom: {secretKeyRef: {name: rhapsode, key: openrouter-api-key}}
```

Every option is in [`charts/rhapsode/values.yaml`](charts/rhapsode/values.yaml). Among them:

- `homerNamespace`: install rhapsode somewhere other than Homer's namespace.
- `trustedCA`: extra CA certificates for a private GitLab or a TLS-inspecting proxy. The CAs come from a ConfigMap, are appended to the public roots, and are used by both the Python side and the bundled CLI. `create: true` creates an OpenShift `trusted-ca` ConfigMap with the injection label.
- `serviceAccount.annotations`: for IRSA with Bedrock.

### CI

`.github/workflows/ci.yml`:

- **Every PR:** runs ruff and pytest, lints and renders the chart through `kubeconform`, and builds the image for `linux/amd64` and `linux/arm64`.
- **`main`:**
  - image `ghcr.io/barakplasma/rhapsode:sha-<7>` and `:latest`, with SBOM and provenance attestations;
  - chart `oci://ghcr.io/barakplasma/charts/rhapsode:<version>-sha-<7>`, whose `appVersion` is that image tag.
- **`v*` tags:** semver image tags, and a chart with the same version.

## Config env vars

| Var | Default |
|---|---|
| `PUBLIC_INGRESS_CLASSES` | empty (any class) |
| `EXCLUDE_NAMESPACES` | `kube-system` |
| `EXCLUDE_HOST_SUFFIXES` | `.svc,.local,.internal,.cluster.local` |
| `FORCE_HTTPS` | `false`. Set it when TLS ends upstream (e.g. Cloudflare Tunnel) |
| `HOMER_NAMESPACE`, `HOMER_CONFIGMAP`, `CACHE_CONFIGMAP` | `homer`, `homer-config`, `rhapsode-cache` |
| `HOMER_BASE_CONFIG` | `/config/base.yml` |
| `OUTPUT_PATH` | empty. Set it to write a file instead of the ConfigMap |
| `RECONCILE_INTERVAL_S`, `FAILURE_BACKOFF_S` | `60`, `1800` |
| `AGENT_CONCURRENCY`, `AGENT_MAX_TURNS`, `AGENT_MAX_BUDGET_USD`, `AGENT_TIMEOUT_S` | `3`, `8`, `0.05`, `180` |
| `AGENT_MODEL` | SDK default |
| `ARGOCD_NAMESPACE` | `argocd` |
| `GITHUB_TOKEN` | empty. Needed only for private repos |
| `GITLAB_URL`, `GITLAB_TOKEN` | empty |
| `PROBE_VERIFY_TLS` | `true` |
| `SSL_CERT_FILE`, `NODE_EXTRA_CA_CERTS` | set by the chart's `trustedCA` |
