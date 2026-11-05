import asyncio

import pytest
import yaml

from rhapsode import agent, controller, homer, tools
from rhapsode.config import Config
from rhapsode.discovery import Endpoint, _public, group_backends

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
    assert agent.clean(_raw(smart_card="Gatus"), HOSTS, CONFIRMED)["smart_card"] == "Gatus"
    assert agent.clean_ns({"title": "", "icon": "bad"}, "byos-laravel") == {
        "title": "byos-laravel", "icon": "fas fa-layer-group"}


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


def test_reconcile_caches_backs_off_and_merges_hosts(monkeypatch):
    cache, published, calls = {}, [], []

    async def fake_describe(cfg, b, hosts):
        calls.append(tuple(b.hosts))
        if b.hosts[0].startswith("bad"):
            raise RuntimeError("boom")
        return {"name": "Good", "subtitle": "", "category": "Dev", "icon": "fas fa-x", "confidence": 1,
                "hide": False, "primary_host": b.hosts[-1], "api_docs": [{"path": "/docs", "kind": "swagger-ui"}],
                "smart_card": ""}

    async def fake_ns(cfg, ns, cards):
        calls.append(("ns", ns))
        return {"title": "Namespace", "icon": "fas fa-cube"}

    monkeypatch.setattr(controller, "discover",
                        lambda cfg: [_ep("good1.example.com"), _ep("good2.example.com"), _ep("bad.example.com", "b")])
    monkeypatch.setattr(controller, "backend_images", lambda b: ["img:1"])
    monkeypatch.setattr(controller, "describe", fake_describe)
    monkeypatch.setattr(controller, "name_namespace", fake_ns)
    monkeypatch.setattr(homer, "load_cache", lambda cfg: dict(cache))
    monkeypatch.setattr(homer, "save_cache", lambda cfg, d: cache.clear() or cache.update(d))
    monkeypatch.setattr(homer, "publish", lambda cfg, files: published.append(files))
    monkeypatch.setattr(controller, "_failed", {})
    monkeypatch.setattr(controller, "_last_published", None)

    asyncio.run(controller.reconcile(CFG))
    assert sorted(calls) == [("bad.example.com",), ("good1.example.com", "good2.example.com"), ("ns", "ns")]
    assert len(cache) == 2  # the good backend and the namespace title; the failure is not cached
    home = yaml.safe_load(published[0]["config.yml"])
    assert [g["name"] for g in home["services"]] == ["Namespace"]
    good = next(i for i in home["services"][0]["items"] if i["name"] == "Good")
    assert good["url"] == "https://good2.example.com"  # the agent's primary host
    assert [q["url"] for q in good["quick"]] == ["https://good1.example.com", "https://good2.example.com/docs"]
    assert any(i["name"] == "bad" for i in home["services"][0]["items"])  # failure listed with fallback label

    asyncio.run(controller.reconcile(CFG))
    assert len(calls) == 3  # cached, and the failure is in backoff: no new agent runs
    assert len(published) == 1  # unchanged output is not re-published
    assert len(cache) == 2  # cache hits survive the rewrite

    monkeypatch.setattr(controller, "backend_images", lambda b: ["img:2"])  # a release
    asyncio.run(controller.reconcile(CFG))
    assert calls[-1] == ("good1.example.com", "good2.example.com")


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
