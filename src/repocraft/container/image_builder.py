from __future__ import annotations

import io
import logging
import os
import re

import docker
import docker.errors

logger = logging.getLogger(__name__)

BASE_IMAGE = "repocraft-base:latest"

DOCKERFILE = """\
FROM ubuntu:24.04

ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG ALL_PROXY=""
ENV http_proxy=$HTTP_PROXY https_proxy=$HTTPS_PROXY all_proxy=$ALL_PROXY
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \\
    git \\
    curl \\
    wget \\
    build-essential \\
    python3 \\
    python3-pip \\
    python3-venv \\
    python3-dev \\
    nodejs \\
    npm \\
    ca-certificates \\
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.cargo/bin:/root/.local/bin:$PATH"

# Clear proxy env after build so runtime isn't affected
ENV http_proxy="" https_proxy="" all_proxy=""

WORKDIR /workspace

CMD ["/bin/bash"]
"""

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
            # Normalise to uppercase key for the Dockerfile ARG
            buildargs[key.upper()] = rewritten
            if rewritten != val:
                logger.debug("Proxy rewritten for Docker build: %s -> %s", val, rewritten)
    return buildargs


def ensure_base_image(client: docker.DockerClient) -> None:
    try:
        client.images.get(BASE_IMAGE)
        logger.debug("Base image %s already exists", BASE_IMAGE)
        return
    except docker.errors.ImageNotFound:
        pass

    logger.info("Building base Docker image %s ...", BASE_IMAGE)
    buildargs = _collect_proxy_buildargs()
    if buildargs:
        logger.debug("Build args (proxy): %s", list(buildargs.keys()))

    dockerfile_bytes = DOCKERFILE.encode()
    fileobj = io.BytesIO(dockerfile_bytes)

    # Stream build output so errors are visible
    build_log: list[str] = []
    try:
        for chunk in client.api.build(
            fileobj=fileobj,
            tag=BASE_IMAGE,
            rm=True,
            buildargs=buildargs,
            decode=True,
        ):
            if "stream" in chunk:
                line = chunk["stream"].rstrip()
                if line:
                    logger.debug("BUILD: %s", line)
                    build_log.append(line)
            elif "error" in chunk:
                raise docker.errors.BuildError(chunk["error"], iter(build_log))
    except docker.errors.BuildError:
        raise
    except Exception as e:
        raise docker.errors.BuildError(str(e), iter(build_log)) from e

    logger.info("Base image built successfully")
