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


def repo_target(cfg: Config, repo_url: str, path: str) -> tuple[str, str, str] | str:
    """Map a repo URL to (forge, api base, file path), or an error string. Fixed host list, no traversal."""
    u = urlparse(repo_url if "://" in repo_url else f"https://{repo_url}")
    repo = u.path.split("/-/")[0].strip("/").removesuffix(".git")  # GitLab puts tree/blob after /-/
    path = path.strip("/")
    if ".." in path.split("/") or repo.count("/") < 1:
        return "refused: bad repo url or path"
    if u.hostname == "github.com":
        owner_repo = "/".join(repo.split("/")[:2])
        return "github", f"https://api.github.com/repos/{owner_repo}", path
    gl = urlparse(cfg.gitlab_url).hostname if cfg.gitlab_url else None
    if gl and u.hostname == gl:
        return "gitlab", f"{cfg.gitlab_url}/api/v4/projects/{quote(repo, safe='')}", path
    return "refused: only github.com" + (f" and {gl}" if gl else "") + " repos"


async def fetch_repo(cfg: Config, repo_url: str, path: str) -> dict | str:
    t = repo_target(cfg, repo_url, path)
    if isinstance(t, str):
        return t
    forge, api, path = t
    if forge == "github":
        hdr = {"Accept": "application/vnd.github+json"}
        if cfg.github_token:
            hdr["Authorization"] = f"Bearer {cfg.github_token}"
        file_url = f"{api}/contents/{quote(path)}" if path else f"{api}/readme"
        file_hdr = {"Accept": "application/vnd.github.raw+json"}
    else:
        hdr = {"PRIVATE-TOKEN": cfg.gitlab_token} if cfg.gitlab_token else {}
        file_url = f"{api}/repository/files/{quote(path or 'README.md', safe='')}/raw"
        file_hdr = {}
    async with httpx.AsyncClient(timeout=10, headers=hdr) as c:
        p = await c.get(api)
        if p.status_code != 200:
            return f"{forge} {p.status_code}"
        f = await c.get(file_url, headers=file_hdr, params={} if forge == "github" else {"ref": "HEAD"})
    meta = p.json()
    return {"description": meta.get("description"), "topics": meta.get("topics"),
            "file": path or "README", "content": f.text[:2000] if f.status_code == 200 else f"({f.status_code})"}


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
            "service_annotations": {
                k: v for k, v in (svc.metadata.annotations or {}).items() if "last-applied" not in k
            },
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

    @tool(
        "get_repo",
        "Description, topics and one file (README by default) of a GitHub or GitLab repo. "
        "path is a file in the repo (e.g. apps/chhoto/values.yaml), or empty for the README.",
        {"repo_url": str, "path": str},
    )
    async def get_repo(args):
        try:
            return _txt(await fetch_repo(cfg, args["repo_url"], args.get("path") or ""))
        except httpx.HTTPError as e:
            return _txt(f"unreachable: {type(e).__name__}")

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
        return _txt({"status": r.status_code, "redirect_to": r.headers.get("location"),
                     "server": r.headers.get("server"),
                     "title": title.group(1).strip() if title else None,
                     "text": re.sub(r"\s+", " ", text)[:800]})

    server = create_sdk_mcp_server("disc", tools=[get_workload, get_argocd_app, get_repo, probe_url])
    return server, [f"mcp__disc__{t}" for t in ("get_workload", "get_argocd_app", "get_repo", "probe_url")]
