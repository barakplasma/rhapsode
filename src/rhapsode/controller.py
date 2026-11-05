"""Reconcile loop: discover -> group by backend -> (LLM only for new/changed backends and namespaces) -> render
-> publish."""
import asyncio
import hashlib
import json
import logging
import time

from kubernetes import config as kconfig

from . import homer
from .agent import describe, name_namespace
from .config import Config
from .discovery import Backend, backend_images, discover, group_backends

log = logging.getLogger("rhapsode")

# cache key -> monotonic time of the last failed agent run; in memory, so a restart retries everything once
_failed: dict[str, float] = {}
_last_published: dict[str, str] | None = None


def _h(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:16]


def _key(b: Backend) -> str:
    # LLM re-runs only when something that identifies the service changes, including a new image (a release)
    ident = [[e.url for e in b.endpoints], b.service, b.namespace, b.port, b.images,
             [e.annotations for e in b.endpoints]]
    return f"b.{b.key_id}.{_h(ident)}"


def _ns_key(ns: str, cards: list[dict]) -> str:
    return f"ns.{ns}.{_h(sorted((c['name'], c.get('subtitle', ''), c.get('category', '')) for c in cards))}"


def _fallback(b: Backend) -> dict:
    e = b.endpoints[0]
    return {"name": e.host.split(".")[0], "subtitle": e.source, "category": "Other", "icon": "fas fa-link",
            "primary_host": e.host, "api_docs": [], "smart_card": ""}


def card(b: Backend, info: dict) -> dict:
    """Homer card for a backend: the agent's label at the primary host, other hosts and API docs as pills."""
    primary = next((e for e in b.endpoints if e.host == info.get("primary_host")), b.endpoints[0])
    url = primary.url.rstrip("/")
    # shortcut: docs are linked on the primary host's root, so a Service exposed under a sub-path links wrongly;
    # carry the Ingress path prefix through if that ever happens
    docs = [{"url": url + d["path"], "kind": d["kind"], "title": d.get("title", "")} for d in info.get("api_docs", [])]
    return {**{k: v for k, v in info.items() if k not in ("primary_host", "api_docs", "hide", "confidence")},
            "namespace": b.namespace, "host": primary.host, "url": primary.url,
            "aliases": [(e.host, e.url) for e in b.endpoints if e is not primary], "api_docs": docs}


async def reconcile(cfg: Config) -> None:
    global _last_published
    endpoints = await asyncio.to_thread(discover, cfg)
    backends = group_backends(endpoints)
    for b in backends:
        b.images = await asyncio.to_thread(backend_images, b)
    cache = await asyncio.to_thread(homer.load_cache, cfg)
    hosts = {e.host for e in endpoints}
    sem = asyncio.Semaphore(cfg.agent_concurrency)
    new_cache: dict[str, str] = {}

    async def cached(k: str, what: str, run, fallback: dict) -> dict:
        """Cache hit, or fallback while a recent failure backs off, or one agent run (cached on success)."""
        if k in cache:
            new_cache[k] = cache[k]
            return json.loads(cache[k])
        if time.monotonic() - _failed.get(k, -cfg.failure_backoff_s) < cfg.failure_backoff_s:
            return fallback
        async with sem:
            try:
                out = await asyncio.wait_for(run(), cfg.agent_timeout_s)
            except Exception:
                log.exception("agent failed for %s; fallback, retry in %ss", what, cfg.failure_backoff_s)
                _failed[k] = time.monotonic()
                return fallback
        _failed.pop(k, None)
        new_cache[k] = json.dumps(out)
        log.info("described %s -> %s", what, out.get("name") or out.get("title"))
        return out

    async def one(b: Backend) -> dict | None:
        info = await cached(_key(b), ",".join(b.hosts), lambda: describe(cfg, b, hosts), _fallback(b))
        return None if info.get("hide") else card(b, info)

    cards = [c for c in await asyncio.gather(*(one(b) for b in backends)) if c]

    by_ns: dict[str, list[dict]] = {}
    for c in cards:
        by_ns.setdefault(c["namespace"], []).append(c)
    titles = await asyncio.gather(*(
        cached(_ns_key(ns, cs), f"namespace {ns}", lambda ns=ns, cs=cs: name_namespace(cfg, ns, cs),
               {"title": ns, "icon": "fas fa-layer-group"})
        for ns, cs in by_ns.items()))
    ns_meta = dict(zip(by_ns, titles, strict=True))

    files = homer.render(cfg, cards, ns_meta)
    if files != _last_published:
        await asyncio.to_thread(homer.publish, cfg, files)
        _last_published = files
        log.info("published %d services in %d namespaces, %d pages", len(cards), len(by_ns), len(files))
    if new_cache != cache:
        await asyncio.to_thread(homer.save_cache, cfg, new_cache)


def load_kube() -> None:
    try:
        kconfig.load_incluster_config()
    except kconfig.ConfigException:
        kconfig.load_kube_config()


async def run(cfg: Config) -> None:
    while True:
        try:
            await reconcile(cfg)
        except Exception:
            log.exception("reconcile failed")
        await asyncio.sleep(cfg.interval_s)
