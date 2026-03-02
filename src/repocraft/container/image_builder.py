from __future__ import annotations

import os
import re

_LOCALHOST_RE = re.compile(r"(https?://|socks5?://)(127\.0\.0\.1|localhost)(:\d+)", re.IGNORECASE)


def _rewrite_proxy_for_docker(url: str) -> str:
    """Replace 127.0.0.1/localhost with host.docker.internal so the container can reach the host proxy."""
    return _LOCALHOST_RE.sub(r"\1host.docker.internal\3", url)


def _collect_proxy_buildargs() -> dict[str, str]:
    buildargs: dict[str, str] = {}
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        val = os.environ.get(key, "")
        if val:
            rewritten = _rewrite_proxy_for_docker(val)
            buildargs[key.upper()] = rewritten
    return buildargs
