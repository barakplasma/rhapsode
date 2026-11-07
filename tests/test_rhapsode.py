import asyncio
import dataclasses

import pytest
import yaml

from rhapsode import agent, approval, controller, discovery, homer, tools
from rhapsode.config import Config
from rhapsode.discovery import (
    Endpoint,
    _public,
    group_backends,
    merge_by_workload,
    resolve_traefik,
    route_hosts,
    traefik_backends,
)

CFG = Config(ingress_classes=["cloudflare-tunnel"], gitlab_url="https://gitlab.example.com",
             base_config_path="/nonexistent")


@pytest.mark.parametrize("host,ns,ann,klass,want", [
    ("a.example.com", "app", {}, "cloudflare-tunnel", True),
    ("a.example.com", "app", {}, "traefik", False),
    ("a.example.com", "kube-system", {}, "cloudflare-tunnel", False),
    ("*.example.com", "app", {}, "cloudflare-tunnel", False),
    ("", "app", {}, "cloudflare-tunnel", False),
    ("svc.cluster.local", "app", {}, "cloudflare-tunnel", False),
    ("a.example.com", "app", {"rhapsode/ignore": "true"}, "cloudflare-tunnel", False),
    ("a.example.com", "app", {"rhapsode/ignore": "false"}, "traefik", True),
])
def test_public(host, ns, ann, klass, want):
    assert _public(CFG, host, ns, ann, klass) is want


HOSTS = ["grafana.example.com", "dashboard.example.com"]
CONFIRMED = {"/swagger": {"kind": "swagger-ui", "title": "Grafana HTTP API"}, "/openapi.json": {"kind": "openapi"}}


def _raw(**kw):
    return {"name": "Grafana", "subtitle": "s", "category": "Monitoring", "icon": "fas fa-chart-line",
            "confidence": 0.9, "hide": False, "primary_host": "grafana.example.com", "api_docs": ["/swagger"],
            "smart_card": "", **kw}


def test_clean_rejects_bad_icon_and_category_and_clamps():
    out = agent.clean(_raw(name="x" * 50, category="Evil", icon='fas fa-x" onload=1', hide="yes"), HOSTS, CONFIRMED)
    assert out["name"] == "x" * 24
    assert out["category"] == "Other"
    assert out["icon"] == "fas fa-link"
    assert out["hide"] is False
    assert agent.clean(_raw(icon="fab fa-github"), HOSTS, CONFIRMED)["icon"] == "fab fa-github"
    with pytest.raises(ValueError):
        agent.clean(_raw(name=""), HOSTS, CONFIRMED)


def test_clean_only_trusts_tool_confirmed_docs_hosts_and_cards():
    out = agent.clean(_raw(primary_host="evil.example.net", smart_card="PiHole",
                           api_docs=["/swagger", "/made-up", "https://evil.example.net/x", "/swagger"]),
                      HOSTS, CONFIRMED)
    assert out["primary_host"] == HOSTS[0]  # not one of this backend's hosts -> first host
    assert out["smart_card"] == ""  # not in the allowlist
    assert out["api_docs"] == [{"path": "/swagger", "kind": "swagger-ui", "title": "Grafana HTTP API"}]
    twin = ["ghcr.io/twin/gatus:v5.12"]
    assert agent.clean(_raw(smart_card="Gatus"), HOSTS, CONFIRMED, twin)["smart_card"] == "Gatus"
    assert agent.clean(_raw(smart_card="Olivetin"), HOSTS, CONFIRMED, ["ghcr.io/olivetin/OliveTin:2025"])[
        "smart_card"] == "Olivetin"
    assert agent.clean_ns({"title": "", "icon": "bad"}, "byos-laravel") == {
        "title": "byos-laravel", "icon": "fas fa-layer-group"}


@pytest.mark.parametrize("images", [
    ["ghcr.io/barakplasma/hazarot:sha-0249c07"],  # live: the agent picked Gatus for a rehearsal chat app
    ["ghcr.io/twin/gatus-proxy-not:1", "ghcr.io/x/olivetin:1"],  # the other product's image does not count
    [],  # nothing known about the image: no smart card
])
def test_smart_card_needs_an_image_that_says_so(images):
    assert agent.clean(_raw(smart_card="Gatus"), HOSTS, CONFIRMED, images)["smart_card"] == (
        "Gatus" if any("gatus" in i for i in images) else "")


@pytest.mark.parametrize("images,want", [
    (["ghcr.io/barakplasma/hazarot:sha-0249c07"], ""),  # live: Hazarot's cached label says Gatus
    ([], ""),
    (["ghcr.io/twin/gatus:v5.12"], "Gatus"),
])
def test_a_cached_smart_card_is_rechecked_when_the_card_is_built(images, want):
    b = _backend("ns", "svc", (), "x.example.com")
    b.images = images
    stale = {"name": "Hazarot", "subtitle": "s", "category": "Other", "icon": "fas fa-x", "confidence": 1,
             "hide": False, "primary_host": "x.example.com", "api_docs": [], "smart_card": "Gatus"}
    assert controller.card(b, stale, {"x.example.com"})["smart_card"] == want
    assert controller.card(b, {**stale, "smart_card": ["Gatus"]}, {"x.example.com"})["smart_card"] == ""


@pytest.mark.parametrize("body,want", [
    ('{"openapi": "3.1.0", "info": {"title": "Queue API"}}', ("openapi", "Queue API")),
    ("openapi: 3.0.0\ninfo:\n  title: Weather\n", ("openapi", "Weather")),
    ('{"swagger": "2.0", "info": {"title": "Old"}}', ("swagger", "Old")),
    ("<html><title>Swagger UI</title><script>SwaggerUIBundle({})</script>", ("swagger-ui", "Swagger UI")),
    ("<html><title>Docs</title><redoc spec-url=x></redoc>", ("redoc", "Docs")),
    ("<html><title>My SPA</title><div id=app></div>", None),  # SPA catch-all for any path
    ('{"items": []}', None),
])
def test_classify_doc(body, want):
    assert tools.classify_doc(body) == want


def _backend(ns, svc, sel, *hosts, port=80):
    b = group_backends([Endpoint(f"https://{h}", h, "Ingress x/y", ns, svc, {}, port) for h in hosts])[0]
    b.selector = tuple(sel)
    return b


@pytest.mark.parametrize("match,want", [
    ("Host(`a.example.com`)", ["a.example.com"]),
    ("Host(`A.Example.com`) && PathPrefix(`/api`)", ["a.example.com"]),
    ("Host(`a.example.com`, `b.example.com`)", ["a.example.com", "b.example.com"]),
    ("Host(`a.example.com`) || Host(`b.example.com`)", ["a.example.com", "b.example.com"]),
    ("HostRegexp(`{sub:[a-z]+}.example.com`)", []),  # not a hostname
    ("HostSNI(`a.example.com`)", []),
    ("PathPrefix(`/only`)", []),
])
def test_route_hosts(match, want):
    assert route_hosts(match) == want


def _ir(ns, name, *routes):
    return {"metadata": {"namespace": ns, "name": name}, "spec": {"routes": list(routes)}}


def _route(match, *services, kind="Rule"):
    return {"kind": kind, "match": match, "services": list(services)}


def test_traefik_backends():
    items = [
        _ir("b", "later", _route("Host(`a.example.com`)", {"name": "from-later", "port": 80})),
        _ir("a", "early", _route("Host(`a.example.com`) && PathPrefix(`/api`)", {"name": "api", "port": 81}),
            _route("Host(`a.example.com`)", {"name": "web", "namespace": "other", "port": "http"})),
        _ir("a", "special", _route("Host(`t.example.com`)", {"name": "api@internal", "kind": "TraefikService"}),
            _route("Host(`m.example.com`)", {"name": "mw", "kind": "Middleware-ish"}),
            _route("Host(`c.example.com`)", {"name": "c", "port": 80}, {"name": "c-canary", "port": 80}),
            _route("Host(`x.example.com`)", {"name": "x"}, kind="Other")),
        {"metadata": {"namespace": "a", "name": "empty"}, "spec": {}},
    ]
    got = traefik_backends(items)
    # the whole-host rule beats the path-limited one, whatever the order; its Service namespace override applies
    assert got["a.example.com"] == ("other", "web", "http", "a/early")
    assert got["c.example.com"] == ("a", "c", 80, "a/special")  # first Service, default namespace = the route's
    assert "t.example.com" not in got and "m.example.com" not in got and "x.example.com" not in got
    assert traefik_backends(items[::-1]) == got  # list order does not matter


def test_a_whole_host_traefikservice_rule_is_not_overridden_by_a_narrower_service_rule():
    items = [_ir("x", "r",
                 _route("Host(`a.example.com`)", {"name": "wrr", "kind": "TraefikService"}),
                 _route("Host(`a.example.com`) && PathPrefix(`/api`)", {"name": "api", "port": 80}))]
    assert traefik_backends(items) == {}  # "/" is served by something that is not a Service: don't guess "api"


def test_a_host_with_only_path_limited_rules_resolves_only_if_they_agree():
    def one(*rules):
        return traefik_backends([_ir("x", "r", *[_route(f"Host(`a.example.com`) && PathPrefix(`{p}`)", svc)
                                                  for p, svc in rules])])

    y, z = {"name": "y", "port": 80}, {"name": "z", "port": 80}
    assert one(("/y", y), ("/z", z)) == {}  # two apps share the host: nothing says which one the host is
    assert one(("/y", y), ("/y2", y)) == {"a.example.com": ("x", "y", 80, "x/r")}
    assert one(("/y", y), ("/t", {"name": "t", "kind": "TraefikService"})) == {}


def test_a_route_into_an_excluded_namespace_is_not_followed():
    ep = Endpoint("https://t.example.com", "t.example.com", "Ingress apps/t", "apps", "traefik-proxy", {}, 80)
    routes = {"t.example.com": ("kube-system", "traefik-dashboard", 9000, "apps/t")}
    resolve_traefik([ep], {("apps", "traefik-proxy")}, routes, exclude=["kube-system"])
    assert (ep.namespace, ep.backend_service, ep.via) == ("apps", "traefik-proxy", "")  # as before the change
    resolve_traefik([ep], {("apps", "traefik-proxy")}, routes, exclude=[])
    assert (ep.namespace, ep.backend_service) == ("kube-system", "traefik-dashboard")


def test_a_hostname_containing_path_is_not_a_path_limited_rule():
    # If "Pathfinder" in the hostname were read as a path limit, the first rule would count as path-limited, the two
    # rules would name different Services, and the host would stay unresolved. As a whole-host rule it decides alone.
    items = [_ir("x", "a", _route("Host(`Pathfinder.example.com`)", {"name": "pf", "port": 80})),
             _ir("x", "b", _route("Host(`pathfinder.example.com`) && PathPrefix(`/api`)", {"name": "api"}))]
    assert traefik_backends(items) == {"pathfinder.example.com": ("x", "pf", 80, "x/a")}


def test_route_hosts_accepts_double_quotes_and_a_missing_match():
    assert route_hosts('Host("a.example.com") && PathPrefix("/x")') == ["a.example.com"]
    assert route_hosts("Host(`a.example.com`, \"b.example.com\")") == ["a.example.com", "b.example.com"]
    assert traefik_backends([_ir("x", "r", {"kind": "Rule", "match": None, "services": [{"name": "s"}]})]) == {}


def test_resolve_traefik_makes_the_claude_otel_grafana_one_backend():
    def ep(host, ns, svc, port=80):
        return Endpoint(f"https://{host}", host, f"Ingress {ns}/{host}", ns, svc, {}, port)

    eps = [
        ep("grafana.example.com", "claude-otel", "grafana"),
        ep("dashboard.example.com", "claude-otel", "grafana"),
        ep("claude-otel.example.com", "claude-otel", "traefik-proxy"),  # fronts Traefik: ExternalName
        ep("convertx.example.com", "convertx", "traefik-proxy"),
        ep("nowhere.example.com", "convertx", "traefik-proxy"),  # ExternalName with no IngressRoute: unchanged
        ep("plain.example.com", "plain", "plain-svc"),  # real Service: never touched, even with a route for it
    ]
    externals = {("claude-otel", "traefik-proxy"), ("convertx", "traefik-proxy")}
    routes = {"claude-otel.example.com": ("claude-otel", "grafana", 3000, "claude-otel/grafana-route"),
              "convertx.example.com": ("convertx", "convertx", 3000, "convertx/convertx"),
              "plain.example.com": ("elsewhere", "hijack", 1, "x/y")}
    resolve_traefik(eps, externals, routes)
    assert [(e.namespace, e.backend_service, e.backend_port, e.via) for e in eps] == [
        ("claude-otel", "grafana", 80, ""),
        ("claude-otel", "grafana", 80, ""),
        ("claude-otel", "grafana", 3000, "Traefik IngressRoute claude-otel/grafana-route"),
        ("convertx", "convertx", 3000, "Traefik IngressRoute convertx/convertx"),
        ("convertx", "traefik-proxy", 80, ""),
        ("plain", "plain-svc", 80, ""),
    ]
    out = group_backends(eps)
    assert sorted((b.namespace, b.service, tuple(b.hosts)) for b in out) == [
        ("claude-otel", "grafana", ("claude-otel.example.com", "dashboard.example.com", "grafana.example.com")),
        ("convertx", "convertx", ("convertx.example.com",)),
        ("convertx", "traefik-proxy", ("nowhere.example.com",)),
        ("plain", "plain-svc", ("plain.example.com",))]
    assert "Traefik IngressRoute" in next(b for b in out if b.service == "convertx").as_dict()["endpoints"][0]["via"]


def test_discover_follows_a_traefik_fronting_ingress_to_the_real_service(monkeypatch):
    from types import SimpleNamespace as NS

    def ingress(name, host, svc):
        path = NS(path="/", backend=NS(service=NS(name=svc, port=NS(number=80, name=None))))
        return NS(metadata=NS(namespace="claude-otel", name=name, annotations={}),
                  spec=NS(ingress_class_name="cloudflare-tunnel", tls=[],
                          rules=[NS(host=host, http=NS(paths=[path]))]))

    class Net:
        def list_ingress_for_all_namespaces(self):
            return NS(items=[ingress("g", "grafana.example.com", "grafana"),
                             ingress("d", "dashboard.example.com", "grafana"),
                             ingress("c", "claude-otel.example.com", "traefik-proxy")])

    class Core:
        def list_service_for_all_namespaces(self):
            svc = lambda n, t: NS(metadata=NS(namespace="claude-otel", name=n), spec=NS(type=t))  # noqa: E731
            return NS(items=[svc("grafana", "ClusterIP"), svc("traefik-proxy", "ExternalName")])

    class Custom:
        def list_cluster_custom_object(self, group, version, plural):
            if group != "traefik.io":
                raise discovery.client.ApiException(status=404)  # no Gateway API here
            return {"items": [_ir("claude-otel", "grafana-route", _route(
                "Host(`claude-otel.example.com`)", {"name": "grafana", "port": 3000}))]}

    monkeypatch.setattr(discovery.client, "NetworkingV1Api", Net)
    monkeypatch.setattr(discovery.client, "CoreV1Api", Core)
    monkeypatch.setattr(discovery.client, "CustomObjectsApi", Custom)
    eps = discovery.discover(CFG)
    assert [(e.host, e.backend_service, e.via) for e in eps] == [
        ("claude-otel.example.com", "grafana", "Traefik IngressRoute claude-otel/grafana-route"),
        ("dashboard.example.com", "grafana", ""),
        ("grafana.example.com", "grafana", "")]
    assert [(b.service, len(b.endpoints)) for b in group_backends(eps)] == [("grafana", 3)]


@pytest.mark.parametrize("status,want", [(404, []), (403, [])])
def test_missing_traefik_crd_or_permission_is_not_fatal(monkeypatch, caplog, status, want):
    class Api:
        def list_cluster_custom_object(self, *a):
            raise discovery.client.ApiException(status=status)

    monkeypatch.setattr(discovery.client, "CustomObjectsApi", Api)
    with caplog.at_level("WARNING", logger="rhapsode"):
        assert discovery._ingress_routes() == want
    assert ("403" in caplog.text) == (status == 403)  # only a permission problem is worth a warning


def test_other_traefik_api_errors_are_raised(monkeypatch):
    class Api:
        def list_cluster_custom_object(self, *a):
            raise discovery.client.ApiException(status=500)

    monkeypatch.setattr(discovery.client, "CustomObjectsApi", Api)
    with pytest.raises(discovery.client.ApiException):
        discovery._ingress_routes()


def test_merge_by_workload_joins_services_that_select_the_same_pods():
    grafana, mine = (("app", "grafana"),), (("app", "mine"),)

    def mk():
        return [
            _backend("mon", "grafana", grafana, "grafana.example.com", "dashboard.example.com", port=3000),
            _backend("mon", "claude-otel-grafana", grafana, "claude-otel.example.com", port=3001),
            _backend("mon", "mine", mine, "mine.example.com"),
            _backend("other", "grafana", grafana, "elsewhere.example.com"),  # same selector, other namespace
            _backend("mon", "external", (), "ext-a.example.com"),  # no selector: never merged
            _backend("mon", "external-2", (), "ext-b.example.com"),
        ]

    for backends in (mk(), mk()[::-1]):  # the result does not depend on list order
        out = merge_by_workload(backends)
        assert sorted((b.namespace, b.service, tuple(b.hosts)) for b in out) == [
            ("mon", "claude-otel-grafana", ("claude-otel.example.com", "dashboard.example.com",
                                            "grafana.example.com")),
            ("mon", "external", ("ext-a.example.com",)), ("mon", "external-2", ("ext-b.example.com",)),
            ("mon", "mine", ("mine.example.com",)), ("other", "grafana", ("elsewhere.example.com",))]
        assert next(b for b in out if b.service == "claude-otel-grafana").port == 3001  # the chosen Service's port


def test_group_backends_merges_hosts_of_one_service():
    eps = [Endpoint(f"https://{h}", h, "Ingress mon/x", "mon", "grafana", {}, 3000)
           for h in ("grafana.example.com", "dashboard.example.com", "claude-otel.example.com")]
    eps += [Endpoint("https://a.example.com", "a.example.com", "HTTPRoute x/a", "x", "", {}),
            Endpoint("https://b.example.com", "b.example.com", "HTTPRoute x/b", "x", "", {})]
    bs = group_backends(eps)
    assert [(b.namespace, b.service, len(b.endpoints)) for b in bs] == [
        ("mon", "grafana", 3), ("x", "", 1), ("x", "", 1)]
    assert bs[0].hosts == ["claude-otel.example.com", "dashboard.example.com", "grafana.example.com"]


@pytest.mark.parametrize("url,path,want", [
    ("https://github.com/barakplasma/homelab-manifests.git", "apps/x/values.yaml",
     ("github", "https://api.github.com/repos/barakplasma/homelab-manifests", "apps/x/values.yaml")),
    ("github.com/o/r/tree/main/sub", "", ("github", "https://api.github.com/repos/o/r", "")),
    ("https://gitlab.example.com/g/sub/r/-/tree/main", "/README.md",
     ("gitlab", "https://gitlab.example.com/api/v4/projects/g%2Fsub%2Fr", "README.md")),
])
def test_repo_target(url, path, want):
    assert tools.repo_target(CFG, url, path) == want


@pytest.mark.parametrize("url,path", [
    ("https://evil.example.com/o/r", ""),
    ("http://169.254.169.254/latest/meta", ""),
    ("https://github.com/o/r", "../../etc/passwd"),
    ("https://github.com/o", ""),
])
def test_repo_target_refuses(url, path):
    assert tools.repo_target(CFG, url, path).startswith("refused")


def _card(name, ns, host, **kw):
    return {"name": name, "namespace": ns, "host": host, "url": f"https://{host}", "category": "Tools",
            "icon": "fas fa-x", "subtitle": "s", **kw}


def test_render_pages_groups_pills_and_unique_names():
    cards = [
        _card("Grafana", "mon", "grafana.example.com", category="Monitoring",
              aliases=[("dashboard.example.com", "https://dashboard.example.com")],
              api_docs=[{"url": "https://grafana.example.com/swagger", "kind": "swagger-ui", "title": "HTTP API"}]),
        _card("Grafana", "other", "metrics.example.com", category="Monitoring"),
        _card("Chhoto", "guard", "links.example.com"),
        _card("Guard", "guard", "guard.example.com"),
        _card("Gatus", "gatus", "uptime.example.com", category="Monitoring", smart_card="Gatus"),
    ]
    files = homer.render(CFG, cards, {"guard": {"title": "Guard & links", "icon": "fas fa-shield-alt"}})
    assert set(files) == {"config.yml", "monitoring.yml", "tools.yml", "apis.yml"}
    home = yaml.safe_load(files["config.yml"])
    assert [g["name"] for g in home["services"]] == ["gatus", "Guard & links", "mon", "other"]
    assert [i["name"] for i in home["services"][1]["items"]] == ["Chhoto", "Guard"]
    graf = home["services"][2]["items"][0]
    assert graf["name"] == "Grafana (grafana)" and home["services"][3]["items"][0]["name"] == "Grafana (metrics)"
    assert [q["name"] for q in graf["quick"]] == ["dashboard", "Swagger"]
    gatus = home["services"][0]["items"][0]
    assert gatus["type"] == "Gatus" and "subtitle" not in gatus
    assert [link["url"] for link in home["links"]] == ["#", "#rhapsode/monitoring", "#rhapsode/tools", "#rhapsode/apis"]
    tools_page = yaml.safe_load(files["tools.yml"])
    assert [g["name"] for g in tools_page["services"]] == ["Guard & links"]
    apis = yaml.safe_load(files["apis.yml"])["services"]
    assert apis == [{"name": "mon", "icon": "fas fa-layer-group", "items": [
        {"name": "Grafana (grafana)", "subtitle": "HTTP API", "url": "https://grafana.example.com/swagger",
         "icon": "fas fa-book", "target": "_blank", "tag": "Swagger"}]}]
    assert "apis.yml" not in homer.render(CFG, cards[2:4], {})  # no docs, no APIs page


def _ep(host, svc="svc"):
    return Endpoint(f"https://{host}", host, "Ingress ns/x", "ns", svc, {}, 80)


class Cluster:
    """Fake cluster + agent around controller.reconcile: records agent calls, publishes and the cache."""

    def __init__(self, monkeypatch):
        self.cache, self.published, self.pending, self.calls = {}, [], [], []
        self.images = ["img:1"]
        monkeypatch.setattr(controller, "discover", lambda cfg: [
            _ep("good1.example.com"), _ep("good2.example.com"), _ep("bad.example.com", "b")])
        monkeypatch.setattr(controller, "backend_workload", lambda b: ((), self.images))
        monkeypatch.setattr(controller, "describe", self._describe)
        monkeypatch.setattr(controller, "name_namespace", self._ns)
        monkeypatch.setattr(controller, "auth_hint", self._hint)
        monkeypatch.setattr(homer, "load_cache", lambda cfg: dict(self.cache))
        monkeypatch.setattr(homer, "save_cache", lambda cfg, d: self.cache.clear() or self.cache.update(d))
        monkeypatch.setattr(homer, "publish", lambda cfg, files: self.published.append(files))
        monkeypatch.setattr(homer, "publish_pending", lambda cfg, y: self.pending.append(yaml.safe_load(y)["pending"]))
        monkeypatch.setattr(controller, "_failed", {})
        monkeypatch.setattr(controller, "_last_published", None)
        monkeypatch.setattr(controller, "_last_pending", None)

    async def _describe(self, cfg, b, hosts):
        self.calls.append(tuple(b.hosts))
        if b.hosts[0].startswith("bad"):
            raise RuntimeError("boom")
        return {"name": "Good", "subtitle": "", "category": "Dev", "icon": "fas fa-x", "confidence": 1,
                "hide": False, "primary_host": b.hosts[-1], "api_docs": [{"path": "/docs", "kind": "swagger-ui"}],
                "smart_card": ""}

    async def _ns(self, cfg, ns, cards):
        self.calls.append(("ns", ns, tuple(sorted(c["name"] for c in cards))))
        return {"title": "Namespace", "icon": "fas fa-cube"}

    async def _hint(self, cfg, url):
        return "cloudflare-access" if "good" in url else "no-auth-seen"

    def run(self, *approved):
        asyncio.run(controller.reconcile(dataclasses.replace(CFG, approved_hosts=list(approved))))

    def text(self):
        return "\n".join(self.published[-1].values())


ALL = ("good1.example.com", "good2.example.com", "bad.example.com")


def test_reconcile_caches_backs_off_and_merges_hosts(monkeypatch):
    c = Cluster(monkeypatch)
    c.run(*ALL)
    assert sorted(c.calls) == [("bad.example.com",), ("good1.example.com", "good2.example.com"),
                               ("ns", "ns", ("Good", "bad"))]
    assert len(c.cache) == 2  # the good backend and the namespace title; the failure is not cached
    home = yaml.safe_load(c.published[0]["config.yml"])
    assert [g["name"] for g in home["services"]] == ["Namespace"]
    good = next(i for i in home["services"][0]["items"] if i["name"] == "Good")
    assert good["url"] == "https://good2.example.com"  # the agent's primary host
    assert [q["url"] for q in good["quick"]] == ["https://good1.example.com", "https://good2.example.com/docs"]
    assert any(i["name"] == "bad" for i in home["services"][0]["items"])  # failure listed with fallback label

    c.run(*ALL)
    assert len(c.calls) == 3  # cached, and the failure is in backoff: no new agent runs
    assert len(c.published) == 1  # unchanged output is not re-published
    assert len(c.cache) == 2  # cache hits survive the rewrite

    c.images = ["img:2"]  # a release
    c.run(*ALL)
    assert ("good1.example.com", "good2.example.com") in c.calls[3:]  # relabelled; the namespace title is a hit


def test_two_services_for_one_workload_are_one_card_and_one_agent_run(monkeypatch):
    c = Cluster(monkeypatch)
    monkeypatch.setattr(controller, "discover", lambda cfg: [
        _ep("a.example.com", "grafana"), _ep("b.example.com", "grafana-alt")])
    monkeypatch.setattr(controller, "backend_workload", lambda b: ((("app", "grafana"),), ["img:1"]))
    c.run("a.example.com", "b.example.com")
    assert [x for x in c.calls if x[0] != "ns"] == [("a.example.com", "b.example.com")]
    items = yaml.safe_load(c.published[0]["config.yml"])["services"][0]["items"]
    assert len(items) == 1
    assert [q["name"] for q in items[0]["quick"]] == ["a", "Swagger"]  # the other host, then the API docs


def test_default_deny_publishes_nothing_and_lists_everything_as_pending(monkeypatch):
    c = Cluster(monkeypatch)
    c.run()  # nothing approved
    assert set(c.published[0]) == {"config.yml"}
    assert yaml.safe_load(c.published[0]["config.yml"])["services"] == []
    assert not any(h in c.text() for h in ("good1", "good2", "bad", "Good"))
    assert [p["host"] for p in c.pending[0]] == ["bad.example.com", "good1.example.com", "good2.example.com"]
    assert not any(call[0] == "ns" for call in c.calls)  # no namespace run: no approved card to title


def test_only_approved_hosts_are_published_and_the_rest_are_pending(monkeypatch):
    c = Cluster(monkeypatch)
    c.run("GOOD2.example.com")  # hostnames are case-insensitive
    pages = "\n".join(c.published[0].values())
    assert "good2.example.com" in pages
    assert not any(h in pages for h in ("good1", "bad.example.com", "bad"))  # not in any page, pill or title
    assert ("ns", "ns", ("Good",)) in c.calls  # the namespace run saw approved cards only
    good = yaml.safe_load(c.published[0]["config.yml"])["services"][0]["items"][0]
    assert [q["url"] for q in good["quick"]] == ["https://good2.example.com/docs"]  # no pill for unapproved good1
    assert c.pending == [[
        {"host": "bad.example.com", "namespace": "ns", "service": "b", "images": ["img:1"], "auth": "no-auth-seen",
         # the agent failed for this one, so the suggestion is the fallback label
         "suggested": {"name": "bad", "subtitle": "Ingress ns/x", "category": "Other"}},
        {"host": "good1.example.com", "namespace": "ns", "service": "svc", "images": ["img:1"],
         "auth": "cloudflare-access", "suggested": {"name": "Good", "subtitle": "", "category": "Dev"}}]]

    c.run("good2.example.com")
    assert len(c.pending) == 1 and len(c.published) == 1  # nothing changed: nothing rewritten

    n = len(c.calls)
    c.run("good2.example.com", "good1.example.com")  # approving a host needs no new agent run for its backend
    assert [x for x in c.calls[n:] if x[0] != "ns"] == []
    assert "https://good1.example.com" in [q["url"] for q in yaml.safe_load(
        c.published[1]["config.yml"])["services"][0]["items"][0]["quick"]]
    assert [p["host"] for p in c.pending[-1]] == ["bad.example.com"]


def test_hidden_by_the_agent_is_never_pending(monkeypatch):
    c = Cluster(monkeypatch)
    describe_good = c._describe
    internal = {"name": "Internal", "subtitle": "", "category": "Other", "icon": "fas fa-x", "confidence": 1,
                "hide": True, "primary_host": "bad.example.com", "api_docs": [], "smart_card": ""}

    async def hide_bad(cfg, b, hosts):
        return internal if b.hosts[0].startswith("bad") else await describe_good(cfg, b, hosts)

    monkeypatch.setattr(controller, "describe", hide_bad)
    c.run()
    assert [p["host"] for p in c.pending[0]] == ["good1.example.com", "good2.example.com"]
    c.run("bad.example.com")  # approving it does not publish it either: the agent said it is internal
    assert "bad.example.com" not in c.text()


def test_a_failing_pending_publish_does_not_cost_the_cache(monkeypatch):
    c = Cluster(monkeypatch)

    def boom(cfg, y):
        raise RuntimeError("forbidden")

    monkeypatch.setattr(homer, "publish_pending", boom)
    c.run(*ALL)
    assert len(c.published) == 1 and len(c.cache) == 2  # the dashboard and the cache are saved regardless


@pytest.mark.parametrize("status,location,want", [
    (302, "https://team.cloudflareaccess.com/cdn-cgi/access/login/x", "cloudflare-access"),
    (302, "https://app.example.com/cdn-cgi/access/authorized", "cloudflare-access"),
    (401, "", "401"), (403, "", "403"),
    (302, "/login?next=/", "login-redirect"), (303, "https://sso.example.com/oauth/authorize", "login-redirect"),
    (302, "/dashboard/", "no-auth-seen"), (200, "", "no-auth-seen"), (404, "", "no-auth-seen"),
])
def test_classify_auth(status, location, want):
    assert approval.classify_auth(status, location) == want


def test_auth_hint_against_a_real_server():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/login-wall":
                self.send_response(302)
                self.send_header("Location", "/login?next=/")
            elif self.path == "/401":
                self.send_response(401)
            else:
                self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"

    async def hints():
        return [await approval.auth_hint(CFG, base + p) for p in ("/login-wall", "/401", "/open")]

    try:
        assert asyncio.run(hints()) == ["login-redirect", "401", "no-auth-seen"]
    finally:
        srv.shutdown()
    assert asyncio.run(approval.auth_hint(CFG, base + "/open")) == "no-auth-seen"  # cached: server is gone
    assert asyncio.run(approval.auth_hint(CFG, "http://127.0.0.1:9/x")) == "unreachable"


def test_probe_docs_against_a_real_server():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = {"/openapi.json": '{"openapi": "3.1.0", "info": {"title": "Queue API"}}',
                    "/docs": "<title>Queue - Swagger UI</title><script>SwaggerUIBundle({})</script>"}.get(
                self.path, "<title>SPA</title><div id=app></div>")  # SPA: 200 + index.html for every other path
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        found = asyncio.run(tools.probe_docs(f"http://127.0.0.1:{srv.server_port}"))
    finally:
        srv.shutdown()
    assert found == {"/openapi.json": {"kind": "openapi", "title": "Queue API"},
                     "/docs": {"kind": "swagger-ui", "title": "Queue - Swagger UI"}}
