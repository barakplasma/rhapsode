"""Tools the agent can call. Exposed to the Claude Agent SDK as an in-process MCP server."""
import json
import re
from urllib.parse import quote, urlparse

import httpx
from claude_agent_sdk import create_sdk_mcp_server, tool
from kubernetes import client

from .config import Config

MAX = 3000  # chars returned per tool call; keeps context small


def _txt(obj) -> dict:
    s = obj if isinstance(obj, str) else json.dumps(obj, default=str)
    return {"content": [{"type": "text", "text": s[:MAX]}]}


def build_server(cfg: Config, allowed_hosts: set[str]):
    @tool("get_workload", "Images, ports and labels of the pods behind a Service.", {"namespace": str, "service": str})
    async def get_workload(args):
        core = client.CoreV1Api()
        try:
            svc = core.read_namespaced_service(args["service"], args["namespace"])
            sel = ",".join(f"{k}={v}" for k, v in (svc.spec.selector or {}).items())
            pods = core.list_namespaced_pod(args["namespace"], label_selector=sel, limit=1).items if sel else []
        except client.ApiException as e:
            return _txt(f"k8s error {e.status}")
        pod = pods[0] if pods else None
        return _txt({
            "service_labels": svc.metadata.labels,
            "service_annotations": {k: v for k, v in (svc.metadata.annotations or {}).items() if "last-applied" not in k},
            "ports": [p.port for p in svc.spec.ports or []],
            "images": [c.image for c in pod.spec.containers] if pod else [],
            "pod_labels": pod.metadata.labels if pod else {},
        })

    @tool("get_argocd_app", "Fetch an ArgoCD Application (repoURL, path, chart) by name.", {"name": str})
    async def get_argocd_app(args):
        try:
            app = client.CustomObjectsApi().get_namespaced_custom_object(
                "argoproj.io", "v1alpha1", cfg.argocd_namespace, "applications", args["name"]
            )
        except client.ApiException as e:
            return _txt(f"not found ({e.status})")
        spec = app.get("spec", {})
        srcs = spec.get("sources") or [spec.get("source", {})]
        return _txt({"project": spec.get("project"), "sources": srcs, "dest": spec.get("destination")})

    @tool("get_gitlab_project", "Description and README excerpt of a GitLab repo given its URL.", {"repo_url": str})
    async def get_gitlab_project(args):
        if not cfg.gitlab_url or not args["repo_url"].startswith(cfg.gitlab_url):
            return _txt("repo is not on the configured GitLab")
        path = urlparse(args["repo_url"]).path.strip("/").removesuffix(".git")
        hdr = {"PRIVATE-TOKEN": cfg.gitlab_token} if cfg.gitlab_token else {}
        api = f"{cfg.gitlab_url}/api/v4/projects/{quote(path, safe='')}"
        async with httpx.AsyncClient(timeout=10, headers=hdr) as c:
            p = await c.get(api)
            if p.status_code != 200:
                return _txt(f"gitlab {p.status_code}")
            readme = await c.get(f"{api}/repository/files/README.md/raw", params={"ref": "HEAD"})
        return _txt({"description": p.json().get("description"), "topics": p.json().get("topics"),
                     "readme": readme.text[:1500] if readme.status_code == 200 else ""})

    @tool("probe_url", "GET one of the discovered URLs; returns status, title and a text snippet.", {"url": str})
    async def probe_url(args):
        # SSRF guard: only hosts we discovered ourselves, and no redirects (they could leave that set).
        if urlparse(args["url"]).hostname not in allowed_hosts:
            return _txt("refused: host was not discovered from the cluster")
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=False, verify=cfg.probe_verify_tls) as c:
                r = await c.get(args["url"])
        except httpx.HTTPError as e:
            return _txt(f"unreachable: {type(e).__name__}")
        body = r.text[:20000]
        title = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
        text = re.sub(r"<(script|style).*?</\1>|<[^>]+>", " ", body, flags=re.S | re.I)
        return _txt({"status": r.status_code, "redirect_to": r.headers.get("location"), "server": r.headers.get("server"),
                     "title": title.group(1).strip() if title else None,
                     "text": re.sub(r"\s+", " ", text)[:800]})

    server = create_sdk_mcp_server("disc", tools=[get_workload, get_argocd_app, get_gitlab_project, probe_url])
    return server, [f"mcp__disc__{t}" for t in ("get_workload", "get_argocd_app", "get_gitlab_project", "probe_url")]
