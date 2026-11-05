"""Settings, all from env so the same image runs on k3s with Bedrock or an API key."""
import os
from dataclasses import dataclass, field


def _list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


@dataclass(frozen=True)
class Config:
    # --- what counts as "public"
    # empty = every Ingress/HTTPRoute with a real hostname
    ingress_classes: list[str] = field(default_factory=lambda: _list("PUBLIC_INGRESS_CLASSES"))
    exclude_namespaces: list[str] = field(default_factory=lambda: _list("EXCLUDE_NAMESPACES", "kube-system"))
    exclude_host_suffixes: list[str] = field(
        default_factory=lambda: _list("EXCLUDE_HOST_SUFFIXES", ".svc,.local,.internal,.cluster.local")
    )
    # TLS terminated upstream (e.g. Cloudflare Tunnel): label every URL https
    force_https: bool = os.environ.get("FORCE_HTTPS", "false").lower() == "true"
    # --- output
    homer_namespace: str = os.environ.get("HOMER_NAMESPACE", "homer")
    homer_configmap: str = os.environ.get("HOMER_CONFIGMAP", "homer-config")
    base_config_path: str = os.environ.get("HOMER_BASE_CONFIG", "/config/base.yml")
    output_path: str = os.environ.get("OUTPUT_PATH", "")  # set for local dev; skips the ConfigMap
    cache_configmap: str = os.environ.get("CACHE_CONFIGMAP", "rhapsode-cache")
    # --- loop
    interval_s: int = int(os.environ.get("RECONCILE_INTERVAL_S", "60"))
    agent_concurrency: int = int(os.environ.get("AGENT_CONCURRENCY", "3"))
    max_turns: int = int(os.environ.get("AGENT_MAX_TURNS", "8"))
    max_budget_usd: float = float(os.environ.get("AGENT_MAX_BUDGET_USD", "0.05"))
    agent_timeout_s: int = int(os.environ.get("AGENT_TIMEOUT_S", "180"))
    failure_backoff_s: int = int(os.environ.get("FAILURE_BACKOFF_S", "1800"))
    # --- enrichment sources
    argocd_namespace: str = os.environ.get("ARGOCD_NAMESPACE", "argocd")
    gitlab_url: str = os.environ.get("GITLAB_URL", "").rstrip("/")
    gitlab_token: str = os.environ.get("GITLAB_TOKEN", "")
    github_token: str = os.environ.get("GITHUB_TOKEN", "")  # optional; needed for private repos
    probe_verify_tls: bool = os.environ.get("PROBE_VERIFY_TLS", "true").lower() != "false"
    # --- LLM. Auth is picked up by the SDK from env:
    #   API key   : ANTHROPIC_API_KEY
    #   OpenRouter: ANTHROPIC_BASE_URL=https://openrouter.ai/api + ANTHROPIC_AUTH_TOKEN + ANTHROPIC_API_KEY=""
    #   Bedrock : CLAUDE_CODE_USE_BEDROCK=1 + AWS_REGION + AWS creds (or IRSA/instance role)
    model: str = os.environ.get("AGENT_MODEL", "")  # Bedrock needs an inference-profile id here
