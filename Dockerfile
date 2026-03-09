FROM python:3.12-slim

# Install system deps: Docker CLI (to manage catocode-worker container)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates git \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
       https://download.docker.com/linux/debian bookworm stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install Python deps (layer-cached)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy source
COPY src/ src/

# Data directory
RUN mkdir -p /data
ENV CATOCODE_DB_PATH=/data/catocode.db

EXPOSE 8000

ENTRYPOINT ["uv", "run", "catocode"]
CMD ["server", "--port", "8000"]
