"""Reconcile loop: discover -> (LLM only for new/changed endpoints) -> render -> publish."""
import asyncio
import hashlib
import json
import logging

from kubernetes import client, config as kconfig

from . import homer
from .agent import describe
from .config import Config
from .discovery import Endpoint, discover

log = logging.getLogger("rhapsode")


def _key(ep: Endpoint) -> str:
    # LLM re-runs only when something that identifies the service changes
    raw = json.dumps([ep.url, ep.backend_service, ep.namespace, ep.annotations], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _fallback(ep: Endpoint) -> dict:
    return {"name": ep.host.split(".")[0], "subtitle": ep.source, "category": "Other", "icon": "fas fa-link"}


async def reconcile(cfg: Config) -> None:
    endpoints = await asyncio.to_thread(discover, cfg)
    cache = await asyncio.to_thread(homer.load_cache, cfg)
    hosts = {e.host for e in endpoints}
    sem = asyncio.Semaphore(cfg.agent_concurrency)
    new_cache: dict[str, str] = {}

    async def one(ep: Endpoint) -> dict | None:
        k = f"{ep.host}.{_key(ep)}"
        if k in cache:
            info = json.loads(cache[k])
        else:
            async with sem:
                try:
                    info = await describe(cfg, ep, hosts)
                except Exception:
                    log.exception("agent failed for %s; using fallback (not cached)", ep.host)
                    return {**_fallback(ep), "url": ep.url}
            log.info("described %s -> %s (%.2f)", ep.host, info.get("name"), info.get("confidence", 0))
        new_cache[k] = json.dumps(info)
        return None if info.get("hide") else {**info, "url": ep.url}

    items = [i for i in await asyncio.gather(*(one(e) for e in endpoints)) if i]
    await asyncio.to_thread(homer.publish, cfg, homer.render(cfg, items))
    if new_cache != cache:
        await asyncio.to_thread(homer.save_cache, cfg, new_cache)
    log.info("published %d services", len(items))


async def run(cfg: Config) -> None:
    try:
        kconfig.load_incluster_config()
    except kconfig.ConfigException:
        kconfig.load_kube_config()
    client.ApiClient().configuration.retries = 2
    while True:
        try:
            await reconcile(cfg)
        except Exception:
            log.exception("reconcile failed")
        await asyncio.sleep(cfg.interval_s)
