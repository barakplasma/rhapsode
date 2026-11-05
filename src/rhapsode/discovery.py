"""Find public Ingress and Gateway API HTTPRoute endpoints."""
from dataclasses import dataclass, asdict

from kubernetes import client

from .config import Config

IGNORE = "rhapsode/ignore"  # annotation: "true" hides it, "false" forces it public


@dataclass
class Endpoint:
    url: str
    host: str
    source: str  # "Ingress ns/name" or "HTTPRoute ns/name"
    namespace: str
    backend_service: str
    annotations: dict

    def as_dict(self) -> dict:
        return asdict(self)


def _public(cfg: Config, host: str, ns: str, ann: dict, klass: str | None) -> bool:
    flag = ann.get(IGNORE, "").lower()
    if flag == "true":
        return False
    if flag == "false":
        return True
    if not host or "*" in host or ns in cfg.exclude_namespaces:
        return False
    if any(host.endswith(s) for s in cfg.exclude_host_suffixes):
        return False
    return not cfg.ingress_classes or klass in cfg.ingress_classes


def _ann(meta) -> dict:
    return {k: v for k, v in (meta.annotations or {}).items() if not k.startswith("kubectl.")}


def discover(cfg: Config) -> list[Endpoint]:
    found: dict[str, Endpoint] = {}

    net = client.NetworkingV1Api()
    for ing in net.list_ingress_for_all_namespaces().items:
        ns, name = ing.metadata.namespace, ing.metadata.name
        ann = _ann(ing.metadata)
        klass = ing.spec.ingress_class_name or ann.get("kubernetes.io/ingress.class")
        tls_hosts = {h for t in (ing.spec.tls or []) for h in (t.hosts or [])}
        for rule in ing.spec.rules or []:
            if not _public(cfg, rule.host or "", ns, ann, klass):
                continue
            paths = rule.http.paths if rule.http else []
            # prefer the "/" backend; otherwise the first one
            pick = next((p for p in paths if p.path in (None, "/")), paths[0] if paths else None)
            svc = pick.backend.service.name if pick and pick.backend.service else ""
            scheme = "https" if rule.host in tls_hosts else "http"
            found.setdefault(
                rule.host,
                Endpoint(f"{scheme}://{rule.host}", rule.host, f"Ingress {ns}/{name}", ns, svc, ann),
            )

    # Gateway API is optional: skip quietly if the CRD isn't installed.
    try:
        routes = client.CustomObjectsApi().list_cluster_custom_object(
            "gateway.networking.k8s.io", "v1", "httproutes"
        )["items"]
    except client.ApiException as e:
        if e.status != 404:
            raise
        routes = []
    for r in routes:
        meta = r["metadata"]
        ns, name = meta["namespace"], meta["name"]
        ann = {k: v for k, v in meta.get("annotations", {}).items() if not k.startswith("kubectl.")}
        refs = [b["name"] for rule in r["spec"].get("rules", []) for b in rule.get("backendRefs", [])]
        for host in r["spec"].get("hostnames", []):
            if _public(cfg, host, ns, ann, None):
                found.setdefault(
                    host,
                    Endpoint(f"https://{host}", host, f"HTTPRoute {ns}/{name}", ns, refs[0] if refs else "", ann),
                )
    return sorted(found.values(), key=lambda e: e.host)
