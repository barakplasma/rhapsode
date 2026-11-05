"""Render Homer's config: a Home page grouped by namespace, one page per category, and an APIs page.

Homer has one level of groups. Pages are its way to nest: a navbar link "#rhapsode/ai" makes Homer fetch
assets/rhapsode/ai.yml and swap in its services, and all files live in the one homer-config ConfigMap.
"""
import os

import yaml
from kubernetes import client

from .config import Config

CATEGORY_ICONS = {"Dev": "fas fa-code", "Media": "fas fa-photo-video", "Monitoring": "fas fa-chart-line",
                  "Data": "fas fa-database", "AI": "fas fa-robot", "Infra": "fas fa-server", "Home": "fas fa-home",
                  "Tools": "fas fa-tools", "Other": "fas fa-th-large"}
DOC_PILLS = {"swagger-ui": "Swagger", "redoc": "ReDoc", "scalar": "Scalar", "openapi": "OpenAPI", "swagger": "OpenAPI"}
PAGE_DIR = "rhapsode"  # matches the directory mount /www/assets/rhapsode in Homer


def _label(host: str) -> str:
    return host.split(".")[0]


def unique_names(cards: list[dict]) -> list[dict]:
    """Different backends that got the same name get their primary host label appended."""
    seen: dict[str, int] = {}
    for c in cards:
        seen[c["name"].lower()] = seen.get(c["name"].lower(), 0) + 1
    return [{**c, "name": f'{c["name"]} ({_label(c["host"])})'} if seen[c["name"].lower()] > 1 else c
            for c in cards]


def _item(c: dict) -> dict:
    e = {"name": c["name"], "url": c["url"], "icon": c.get("icon", "fas fa-link"), "target": "_blank",
         "tag": c.get("category") or "Other"}
    if c.get("smart_card"):
        e["type"] = c["smart_card"]  # a subtitle would hide the card's own status line
    else:
        e["subtitle"] = c.get("subtitle", "")
    if c.get("keywords"):
        e["keywords"] = c["keywords"]
    quick = [{"name": _label(h), "url": u, "icon": "fas fa-link", "target": "_blank"} for h, u in c.get("aliases", [])]
    quick += [{"name": DOC_PILLS.get(d["kind"], "API"), "url": d["url"], "icon": "fas fa-book", "target": "_blank"}
              for d in c.get("api_docs", [])]
    if quick:
        e["quick"] = quick
    return e


def _groups(cards: list[dict], ns_meta: dict[str, dict], items_of=lambda c: [_item(c)]) -> list[dict]:
    by_ns: dict[str, list[dict]] = {}
    for c in cards:
        by_ns.setdefault(c["namespace"], []).append(c)
    groups = []
    for ns, cs in by_ns.items():
        meta = ns_meta.get(ns) or {"title": ns, "icon": "fas fa-layer-group"}
        items = [x for c in sorted(cs, key=lambda c: c["name"].lower()) for x in items_of(c)]
        if items:
            groups.append({"name": meta["title"], "icon": meta["icon"], "items": items})
    return sorted(groups, key=lambda g: g["name"].lower())


def _doc_items(c: dict) -> list[dict]:
    return [{"name": c["name"], "subtitle": d.get("title") or DOC_PILLS.get(d["kind"], "API docs"), "url": d["url"],
             "icon": "fas fa-book", "target": "_blank", "tag": DOC_PILLS.get(d["kind"], "API")}
            for d in c.get("api_docs", [])]


def _dump(doc: dict) -> str:
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def render(cfg: Config, cards: list[dict], ns_meta: dict[str, dict]) -> dict[str, str]:
    """{file name: yaml} for the homer-config ConfigMap: config.yml plus one file per extra page."""
    try:
        with open(cfg.base_config_path) as f:
            base = yaml.safe_load(f) or {}
    except FileNotFoundError:
        base = {"title": "Dashboard"}
    cards = unique_names(cards)
    files: dict[str, str] = {}
    links = [{"name": "All", "icon": "fas fa-th", "url": "#"}]
    for cat in sorted({c.get("category") or "Other" for c in cards}):
        slug = cat.lower()
        page = [c for c in cards if (c.get("category") or "Other") == cat]
        files[f"{slug}.yml"] = _dump({"subtitle": cat, "services": _groups(page, ns_meta)})
        links.append({"name": cat, "icon": CATEGORY_ICONS.get(cat, "fas fa-th-large"), "url": f"#{PAGE_DIR}/{slug}"})
    if any(c.get("api_docs") for c in cards):
        files["apis.yml"] = _dump({"subtitle": "API docs", "services": _groups(cards, ns_meta, _doc_items)})
        links.append({"name": "APIs", "icon": "fas fa-book", "url": f"#{PAGE_DIR}/apis"})
    base["links"] = [*(base.get("links") or []), *links]
    base["services"] = _groups(cards, ns_meta)
    files["config.yml"] = _dump(base)
    return files


def _upsert_cm(ns: str, name: str, data: dict[str, str]) -> None:
    core = client.CoreV1Api()
    body = client.V1ConfigMap(metadata=client.V1ObjectMeta(name=name), data=data)
    try:
        core.replace_namespaced_config_map(name, ns, body)
    except client.ApiException as e:
        if e.status != 404:
            raise
        core.create_namespaced_config_map(ns, body)


def publish(cfg: Config, files: dict[str, str]) -> None:
    if cfg.output_path:  # local dev: config.yml at OUTPUT_PATH, page files next to it
        for name, body in files.items():
            path = cfg.output_path if name == "config.yml" else os.path.join(os.path.dirname(cfg.output_path), name)
            with open(path, "w") as f:
                f.write(body)
    else:  # replace, not merge: pages for categories that disappeared go away too
        _upsert_cm(cfg.homer_namespace, cfg.homer_configmap, files)


def load_cache(cfg: Config) -> dict[str, str]:
    try:
        cm = client.CoreV1Api().read_namespaced_config_map(cfg.cache_configmap, cfg.homer_namespace)
        return dict(cm.data or {})
    except client.ApiException as e:
        if e.status == 404:
            return {}
        raise


def save_cache(cfg: Config, data: dict[str, str]) -> None:
    _upsert_cm(cfg.homer_namespace, cfg.cache_configmap, data)
