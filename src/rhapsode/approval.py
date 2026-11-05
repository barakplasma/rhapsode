"""Approval gate helpers: a login hint for hosts that are waiting for approval."""
import re
import time

import httpx

from .config import Config

HINT_TTL_S = 3600
_hints: dict[str, tuple[float, str]] = {}  # url -> (monotonic time, hint)
_LOGIN = re.compile(r"login|signin|sign-in|oauth|/auth|sso", re.I)


def classify_auth(status: int, location: str) -> str:
    """What an unauthenticated GET suggests. A login page rendered in place of a 200 can't be told apart."""
    if "cloudflareaccess.com" in location.lower() or "/cdn-cgi/access/" in location.lower():
        return "cloudflare-access"
    if status in (401, 403):
        return str(status)
    if 300 <= status < 400 and _LOGIN.search(location):
        return "login-redirect"
    return "no-auth-seen"


async def auth_hint(cfg: Config, url: str) -> str:
    """classify_auth for the host's public URL, cached for an hour. 'unreachable' is not cached."""
    hit = _hints.get(url)
    if hit and time.monotonic() - hit[0] < HINT_TTL_S:
        return hit[1]
    try:  # no redirects followed, same as probe_url: a redirect to elsewhere is the answer, not a trail to walk
        async with httpx.AsyncClient(timeout=5, follow_redirects=False, verify=cfg.probe_verify_tls) as c:
            r = await c.get(url)
    except httpx.HTTPError:
        return "unreachable"
    out = classify_auth(r.status_code, r.headers.get("location", ""))
    _hints[url] = (time.monotonic(), out)
    return out
