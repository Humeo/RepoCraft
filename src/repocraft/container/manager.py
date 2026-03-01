from __future__ import annotations

import logging
import os
import re
import tarfile
import io
from dataclasses import dataclass

import docker
import docker.errors
import docker.models.containers

from .image_builder import BASE_IMAGE, ensure_base_image, _rewrite_proxy_for_docker

logger = logging.getLogger(__name__)

MEMORY_LIMIT = "2g"
CPU_PERIOD = 100_000
CPU_QUOTA = 100_000  # 1 CPU


def _container_env() -> dict[str, str]:
    """Forward host proxy settings to the container, rewriting localhost -> host.docker.internal."""
    env: dict[str, str] = {}
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
        self._client = docker.from_env()
        self._container: docker.models.containers.Container | None = None

    def start(self, repo_url: str) -> None:
        ensure_base_image(self._client)
        logger.info("Starting container from image %s", BASE_IMAGE)
        env = _container_env()
        if env:
            logger.debug("Container proxy env: %s", list(env.keys()))
        self._container = self._client.containers.run(
            BASE_IMAGE,
            command="sleep infinity",
            detach=True,
            remove=False,
            mem_limit=MEMORY_LIMIT,
            cpu_period=CPU_PERIOD,
            cpu_quota=CPU_QUOTA,
            network_mode="bridge",
            working_dir="/workspace",
            environment=env,
        )
        logger.debug("Container started: %s", self._container.short_id)

        result = self.exec_sync(f"git clone --depth=1 {repo_url} /workspace/repo")
        if result.exit_code != 0:
            raise RuntimeError(f"git clone failed:\n{result.combined}")
        logger.info("Repository cloned successfully")

    def exec_sync(self, command: str, workdir: str = "/workspace/repo") -> ExecResult:
        if self._container is None:
            raise RuntimeError("Container is not running")
        exit_code, output = self._container.exec_run(
            cmd=["bash", "-c", command],
            workdir=workdir,
            demux=True,
        )
        stdout_bytes, stderr_bytes = output if isinstance(output, tuple) else (output, b"")
        stdout = (stdout_bytes or b"").decode(errors="replace")
        stderr = (stderr_bytes or b"").decode(errors="replace")
        logger.debug("exec [%d]: %s", exit_code, command[:80])
        return ExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr)

    def read_file(self, path: str) -> str:
        if self._container is None:
            raise RuntimeError("Container is not running")
        try:
            bits, _ = self._container.get_archive(path)
            buf = io.BytesIO()
            for chunk in bits:
                buf.write(chunk)
            buf.seek(0)
            with tarfile.open(fileobj=buf) as tf:
                member = tf.getmembers()[0]
                f = tf.extractfile(member)
                if f is None:
                    return ""
                return f.read().decode(errors="replace")
        except docker.errors.NotFound:
            raise FileNotFoundError(f"File not found in container: {path}")

    def write_file(self, path: str, content: str) -> None:
        if self._container is None:
            raise RuntimeError("Container is not running")
        filename = path.split("/")[-1]
        directory = "/".join(path.split("/")[:-1]) or "/"
        content_bytes = content.encode()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo(name=filename)
            info.size = len(content_bytes)
            tf.addfile(info, io.BytesIO(content_bytes))
        buf.seek(0)
        self._container.put_archive(directory, buf)

    def list_files(self, path: str = "/workspace/repo") -> str:
        result = self.exec_sync(f"find {path} -maxdepth 3 -type f | head -100", workdir="/workspace")
        return result.stdout

    def get_diff(self) -> str:
        result = self.exec_sync("git diff HEAD")
        return result.stdout

    def stop(self) -> None:
        if self._container is not None:
            try:
                self._container.stop(timeout=10)
                self._container.remove()
                logger.info("Container stopped and removed")
            except docker.errors.NotFound:
                pass
            except Exception as e:
                logger.warning("Error stopping container: %s", e)
            finally:
                self._container = None
