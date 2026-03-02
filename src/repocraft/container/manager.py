from __future__ import annotations

import asyncio
import io
import logging
import os
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

import docker
import docker.errors
import docker.models.containers

from .image_builder import _collect_proxy_buildargs, _rewrite_proxy_for_docker

logger = logging.getLogger(__name__)

CONTAINER_NAME = "repocraft-worker"
IMAGE_NAME = "repocraft-worker:v1"
MEMORY_LIMIT = os.environ.get("REPOCRAFT_MEM", "8g")
CPU_QUOTA = int(os.environ.get("REPOCRAFT_CPUS", "4")) * 100_000


def _container_env(anthropic_api_key: str, github_token: str) -> dict[str, str]:
    env: dict[str, str] = {
        "ANTHROPIC_API_KEY": anthropic_api_key,
        "GITHUB_TOKEN": github_token,
    }
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        val = os.environ.get(key, "")
        if val:
            env[key] = _rewrite_proxy_for_docker(val)
    return env


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def combined(self) -> str:
        parts = []
        if self.stdout:
            parts.append(self.stdout)
        if self.stderr:
            parts.append(f"[stderr]\n{self.stderr}")
        return "\n".join(parts)


class ContainerManager:
    def __init__(self) -> None:
        try:
            self._client = docker.from_env()
        except docker.errors.DockerException as e:
            raise RuntimeError(f"Cannot connect to Docker: {e}") from e

    def _get_container(self) -> docker.models.containers.Container | None:
        try:
            return self._client.containers.get(CONTAINER_NAME)
        except docker.errors.NotFound:
            return None

    def ensure_running(self, anthropic_api_key: str, github_token: str) -> None:
        """4-state machine: missing→create, exited→start, restarting→wait, running→noop."""
        container = self._get_container()

        if container is None:
            self._build_image_if_needed()
            self._create_and_start(anthropic_api_key, github_token)
            self._write_user_claude_md()
            return

        status = container.status
        if status == "running":
            return
        elif status in ("exited", "stopped", "created"):
            container.start()
        elif status == "restarting":
            container.reload()
        else:
            raise RuntimeError(f"Container in unexpected state: {status}")

    def _build_image_if_needed(self) -> None:
        try:
            self._client.images.get(IMAGE_NAME)
            logger.debug("Image %s already exists", IMAGE_NAME)
            return
        except docker.errors.ImageNotFound:
            pass

        logger.info("Building image %s ...", IMAGE_NAME)
        dockerfile_dir = Path(__file__).parent
        buildargs = _collect_proxy_buildargs()
        build_log: list[str] = []
        try:
            for chunk in self._client.api.build(
                path=str(dockerfile_dir),
                dockerfile="Dockerfile",
                tag=IMAGE_NAME,
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
        logger.info("Image %s built successfully", IMAGE_NAME)

    def _create_and_start(self, anthropic_api_key: str, github_token: str) -> None:
        env = _container_env(anthropic_api_key, github_token)
        self._client.containers.run(
            IMAGE_NAME,
            name=CONTAINER_NAME,
            command="sleep infinity",
            detach=True,
            remove=False,
            mem_limit=MEMORY_LIMIT,
            cpu_period=100_000,
            cpu_quota=CPU_QUOTA,
            network_mode="bridge",
            volumes={
                "repocraft-repos": {"bind": "/repos", "mode": "rw"},
                "repocraft-output": {"bind": "/output", "mode": "rw"},
            },
            environment=env,
        )
        logger.info("Container %s started", CONTAINER_NAME)

    def _write_user_claude_md(self) -> None:
        from ..templates.user_claude_md import get_user_claude_md
        content = get_user_claude_md()
        self.exec("mkdir -p /root/.claude")
        self._put_file("/root/.claude/CLAUDE.md", content)
        logger.debug("User CLAUDE.md written to container")

    def _put_file(self, path: str, content: str) -> None:
        container = self._get_container()
        if container is None:
            raise RuntimeError("Container not running")
        filename = path.split("/")[-1]
        directory = "/".join(path.split("/")[:-1]) or "/"
        content_bytes = content.encode()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo(name=filename)
            info.size = len(content_bytes)
            tf.addfile(info, io.BytesIO(content_bytes))
        buf.seek(0)
        container.put_archive(directory, buf)

    def exec(self, command: str, workdir: str = "/repos") -> ExecResult:
        container = self._get_container()
        if container is None or container.status != "running":
            raise RuntimeError("Container not running")
        exit_code, output = container.exec_run(
            cmd=["bash", "-lc", command],
            workdir=workdir,
            demux=True,
        )
        stdout_bytes, stderr_bytes = output if isinstance(output, tuple) else (output, b"")
        stdout = (stdout_bytes or b"").decode(errors="replace")
        stderr = (stderr_bytes or b"").decode(errors="replace")
        logger.debug("exec [%d]: %s", exit_code, command[:80])
        return ExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr)

    async def exec_stream(self, command: str, workdir: str = "/repos") -> AsyncIterator[str]:
        """Yield stdout lines in real-time using asyncio.Queue + thread executor."""
        container = self._get_container()
        if container is None or container.status != "running":
            raise RuntimeError("Container not running")

        queue: asyncio.Queue[str | None] = asyncio.Queue()
        loop = asyncio.get_event_loop()

        def _stream_thread() -> None:
            exec_id = container.client.api.exec_create(
                container.id,
                cmd=["bash", "-lc", command],
                workdir=workdir,
            )
            for chunk in container.client.api.exec_start(exec_id["Id"], stream=True):
                line = chunk.decode(errors="replace")
                loop.call_soon_threadsafe(queue.put_nowait, line)
            loop.call_soon_threadsafe(queue.put_nowait, None)

        loop.run_in_executor(None, _stream_thread)

        while True:
            line = await queue.get()
            if line is None:
                break
            yield line

    def ensure_repo(self, repo_id: str, repo_url: str) -> None:
        """Clone repo if not present. repo_id = 'owner-repo' slug."""
        result = self.exec(f"test -d /repos/{repo_id}/.git")
        if result.exit_code != 0:
            result = self.exec(f"git clone --depth=50 {repo_url} /repos/{repo_id}")
            if result.exit_code != 0:
                raise RuntimeError(f"git clone failed:\n{result.combined}")
            logger.info("Cloned %s -> /repos/%s", repo_url, repo_id)

    def reset_repo(self, repo_id: str) -> None:
        """Hard reset to origin default branch."""
        workdir = f"/repos/{repo_id}"
        self.exec("git fetch origin", workdir=workdir)
        result = self.exec(
            "git symbolic-ref refs/remotes/origin/HEAD | sed 's@^refs/remotes/origin/@@'",
            workdir=workdir,
        )
        default_branch = result.stdout.strip() or "main"
        self.exec(
            f"git checkout {default_branch} && "
            f"git reset --hard origin/{default_branch} && "
            f"git clean -fdx",
            workdir=workdir,
        )
        logger.info("Reset /repos/%s to origin/%s", repo_id, default_branch)

    def stop(self) -> None:
        """Stop container (do NOT remove — preserve volumes)."""
        container = self._get_container()
        if container is not None:
            try:
                container.stop(timeout=10)
                logger.info("Container %s stopped", CONTAINER_NAME)
            except docker.errors.NotFound:
                pass
