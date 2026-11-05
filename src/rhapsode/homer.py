"""Render Homer's config.yml and publish it."""
import yaml
from kubernetes import client

from .config import Config


def render(cfg: Config, items: list[dict]) -> str:
    try:
        with open(cfg.base_config_path) as f:
            base = yaml.safe_load(f) or {}
    except FileNotFoundError:
        base = {"title": "Dashboard"}

    groups: dict[str, list[dict]] = {}
    for it in sorted(items, key=lambda i: i["name"].lower()):
        entry = {"name": it["name"], "subtitle": it.get("subtitle", ""), "url": it["url"],
                 "icon": it.get("icon", "fas fa-link"), "target": "_blank"}
        for k in ("tag", "keywords"):
            if it.get(k):
                entry[k] = it[k]
        groups.setdefault(it.get("category") or "Other", []).append(entry)

    base["services"] = [{"name": cat, "items": its} for cat, its in sorted(groups.items())]
    return yaml.safe_dump(base, sort_keys=False, allow_unicode=True)


def _upsert_cm(ns: str, name: str, data: dict[str, str]) -> None:
    core = client.CoreV1Api()
    body = client.V1ConfigMap(metadata=client.V1ObjectMeta(name=name), data=data)
    try:
        core.replace_namespaced_config_map(name, ns, body)
    except client.ApiException as e:
        if e.status != 404:
            raise
        core.create_namespaced_config_map(ns, body)


def publish(cfg: Config, yml: str) -> None:
    if cfg.output_path:
        with open(cfg.output_path, "w") as f:
            f.write(yml)
    else:
        _upsert_cm(cfg.homer_namespace, cfg.homer_configmap, {"config.yml": yml})


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
