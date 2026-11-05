"""Reconcile loop: discover -> (LLM only for new/changed endpoints) -> render -> publish."""
import asyncio
import hashlib
import json
import logging
import time

from kubernetes import config as kconfig

from . import homer
from .agent import describe
from .config import Config
from .discovery import Endpoint, discover

log = logging.getLogger("rhapsode")

# cache key -> monotonic time of the last failed agent run; in memory, so a restart retries everything once
_failed: dict[str, float] = {}
_last_published: str | None = None


def _key(ep: Endpoint) -> str:
    # LLM re-runs only when something that identifies the service changes
    raw = json.dumps([ep.url, ep.backend_service, ep.namespace, ep.annotations], sort_keys=True)
    return f"{ep.host}.{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


def _fallback(ep: Endpoint) -> dict:
    return {"name": ep.host.split(".")[0], "subtitle": ep.source, "category": "Other", "icon": "fas fa-link"}


async def reconcile(cfg: Config) -> None:
    global _last_published
    endpoints = await asyncio.to_thread(discover, cfg)
    cache = await asyncio.to_thread(homer.load_cache, cfg)
    hosts = {e.host for e in endpoints}
    sem = asyncio.Semaphore(cfg.agent_concurrency)
    new_cache: dict[str, str] = {}

    async def one(ep: Endpoint) -> dict | None:
        k = _key(ep)
        if k in cache:
            info = json.loads(cache[k])
        elif time.monotonic() - _failed.get(k, -cfg.failure_backoff_s) < cfg.failure_backoff_s:
            return {**_fallback(ep), "url": ep.url}
        else:
            async with sem:
                try:
                    info = await asyncio.wait_for(describe(cfg, ep, hosts), cfg.agent_timeout_s)
                except Exception:
                    log.exception("agent failed for %s; fallback label, retry in %ss", ep.host, cfg.failure_backoff_s)
                    _failed[k] = time.monotonic()
                    return {**_fallback(ep), "url": ep.url}
            _failed.pop(k, None)
            log.info("described %s -> %s (%.2f)", ep.host, info.get("name"), info.get("confidence", 0))
        new_cache[k] = json.dumps(info)
        return None if info.get("hide") else {**info, "url": ep.url}

    items = [i for i in await asyncio.gather(*(one(e) for e in endpoints)) if i]
    yml = homer.render(cfg, items)
    if yml != _last_published:
        await asyncio.to_thread(homer.publish, cfg, yml)
        _last_published = yml
        log.info("published %d services", len(items))
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
