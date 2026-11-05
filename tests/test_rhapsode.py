import asyncio
import json

import pytest
import yaml

from rhapsode import agent, controller, homer, tools
from rhapsode.config import Config
from rhapsode.discovery import Endpoint, _public

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


def test_clean_rejects_bad_icon_and_category_and_clamps():
    out = agent.clean({"name": "x" * 50, "subtitle": "s", "category": "Evil", "icon": "fas fa-x\" onload=1",
                       "confidence": 0.9, "hide": "yes", "tag": "t" * 30})
    assert out["name"] == "x" * 24
    assert out["category"] == "Other"
    assert out["icon"] == "fas fa-link"
    assert out["hide"] is False
    assert out["tag"] == "t" * 12
    assert agent.clean({"name": "A", "icon": "fab fa-github"})["icon"] == "fab fa-github"
    with pytest.raises(ValueError):
        agent.clean({"name": ""})


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


def test_render_groups_and_sorts():
    yml = homer.render(CFG, [
        {"name": "b", "url": "https://b", "category": "Dev", "icon": "fas fa-b"},
        {"name": "A", "url": "https://a", "category": "Dev", "icon": "fas fa-a", "tag": "t"},
        {"name": "c", "url": "https://c", "category": "AI", "icon": "fas fa-c"},
    ])
    doc = yaml.safe_load(yml)
    assert [g["name"] for g in doc["services"]] == ["AI", "Dev"]
    assert [i["name"] for i in doc["services"][1]["items"]] == ["A", "b"]
    assert doc["services"][1]["items"][0]["tag"] == "t"


def _ep(host):
    return Endpoint(f"https://{host}", host, "Ingress ns/x", "ns", "svc", {})


def test_reconcile_caches_and_backs_off(monkeypatch):
    cache, published, calls = {}, [], []

    async def fake_describe(cfg, ep, hosts):
        calls.append(ep.host)
        if ep.host.startswith("bad"):
            raise RuntimeError("boom")
        return {"name": ep.host, "subtitle": "", "category": "Dev", "icon": "fas fa-x", "confidence": 1, "hide": False}

    monkeypatch.setattr(controller, "discover", lambda cfg: [_ep("good.example.com"), _ep("bad.example.com")])
    monkeypatch.setattr(controller, "describe", fake_describe)
    monkeypatch.setattr(homer, "load_cache", lambda cfg: dict(cache))
    monkeypatch.setattr(homer, "save_cache", lambda cfg, d: cache.update(d) or None)
    monkeypatch.setattr(homer, "publish", lambda cfg, y: published.append(y))
    monkeypatch.setattr(controller, "_failed", {})
    monkeypatch.setattr(controller, "_last_published", None)

    asyncio.run(controller.reconcile(CFG))
    assert sorted(calls) == ["bad.example.com", "good.example.com"]
    assert len(cache) == 1 and json.loads(next(iter(cache.values())))["name"] == "good.example.com"
    names = [i["name"] for g in yaml.safe_load(published[0])["services"] for i in g["items"]]
    assert sorted(names) == ["bad", "good.example.com"]  # failure still listed, with the fallback label

    asyncio.run(controller.reconcile(CFG))
    assert len(calls) == 2  # good is cached, bad is in backoff: no new agent runs
    assert len(published) == 1  # unchanged output is not re-published
