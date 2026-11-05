"""Find public Ingress and Gateway API HTTPRoute endpoints, and group them by the Service behind them."""
from dataclasses import asdict, dataclass, field

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
    backend_port: int | str = ""  # Service port number or name; "" when unknown

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Backend:
    """One Service, reached through one or more public hostnames. One agent run and one Homer card each."""
    namespace: str
    service: str  # "" when the route names no Service: then the backend is that single host
    port: int | str
    endpoints: list[Endpoint]
    images: list[str] = field(default_factory=list)
    selector: tuple = ()  # the Service's pod selector, sorted; () when unknown or none

    @property
    def hosts(self) -> list[str]:
        return [e.host for e in self.endpoints]

    @property
    def key_id(self) -> str:
        return f"{self.namespace}.{self.service or self.endpoints[0].host}"

    def as_dict(self) -> dict:
        return {"namespace": self.namespace, "service": self.service, "port": self.port, "images": self.images,
                "endpoints": [{"url": e.url, "source": e.source, "annotations": e.annotations}
                              for e in self.endpoints]}


def group_backends(endpoints: list[Endpoint]) -> list[Backend]:
    """Hosts sharing namespace + Service are one backend (e.g. three hostnames for one Grafana)."""
    out: dict[tuple, Backend] = {}
    for e in endpoints:
        k = (e.namespace, e.backend_service) if e.backend_service else (e.namespace, "", e.host)
        b = out.setdefault(k, Backend(e.namespace, e.backend_service, e.backend_port, []))
        b.endpoints.append(e)
    for b in out.values():
        b.endpoints.sort(key=lambda e: e.host)
    return sorted(out.values(), key=lambda b: b.key_id)


def backend_workload(b: Backend) -> tuple[tuple, list[str]]:
    """(pod selector, container images) behind the Service; ((), []) if unknown. The images make a new release
    get a fresh label, the selector tells that two Services are one workload."""
    if not b.service:
        return (), []
    core = client.CoreV1Api()
    try:
        svc = core.read_namespaced_service(b.service, b.namespace)
        selector = tuple(sorted((svc.spec.selector or {}).items()))
        sel = ",".join(f"{k}={v}" for k, v in selector)
        pods = core.list_namespaced_pod(b.namespace, label_selector=sel, limit=1).items if sel else []
    except client.ApiException:
        return (), []
    return selector, sorted(c.image for c in pods[0].spec.containers) if pods else []


def merge_by_workload(backends: list[Backend]) -> list[Backend]:
    """Services in one namespace with the same pod selector are one workload (a second Service or Ingress for the
    same Grafana): one card, with the extra hostnames as pills. A Service with no selector is never merged."""
    out: dict[object, Backend] = {}
    for b in backends:
        m = out.setdefault((b.namespace, b.selector) if b.selector else id(b), b)
        if m is b:
            continue
        m.endpoints = sorted(m.endpoints + b.endpoints, key=lambda e: e.host)
        if b.service < m.service:  # a stable choice, so the cache key doesn't flap with list order
            m.service, m.port = b.service, b.port
    return sorted(out.values(), key=lambda b: b.key_id)


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
            port = ""
            if pick and pick.backend.service and pick.backend.service.port:
                port = pick.backend.service.port.number or pick.backend.service.port.name or ""
            scheme = "https" if cfg.force_https or rule.host in tls_hosts else "http"
            found.setdefault(
                rule.host,
                Endpoint(f"{scheme}://{rule.host}", rule.host, f"Ingress {ns}/{name}", ns, svc, ann, port),
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
        ann = {k: v for k, v in (meta.get("annotations") or {}).items() if not k.startswith("kubectl.")}
        refs = [b for rule in r["spec"].get("rules", []) for b in rule.get("backendRefs", [])]
        for host in r["spec"].get("hostnames", []):
            if _public(cfg, host, ns, ann, None):
                found.setdefault(
                    host,
                    Endpoint(f"https://{host}", host, f"HTTPRoute {ns}/{name}", ns,
                             refs[0]["name"] if refs else "", ann, refs[0].get("port", "") if refs else ""),
                )
    return sorted(found.values(), key=lambda e: e.host)
