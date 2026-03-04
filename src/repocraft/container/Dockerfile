FROM node:22-slim

ARG HTTP_PROXY="" HTTPS_PROXY="" ALL_PROXY=""
ENV http_proxy=$HTTP_PROXY https_proxy=$HTTPS_PROXY all_proxy=$ALL_PROXY
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# Layer 1: Base OS packages + Python3 (Node.js/npm already included in base image)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl wget ca-certificates gnupg \
    python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Layer 2: GitHub CLI via official apt repo
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# Layer 3: Python uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

# Layer 4: Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Layer 5: Claude Agent SDK (installed system-wide)
RUN pip3 install --break-system-packages claude-agent-sdk

# Layer 6: Playwright Python package + system deps (browsers installed per-user later)
RUN pip3 install --break-system-packages playwright \
    && playwright install-deps chromium

# Layer 7: git credential helper + global git config
# Note: user.name and user.email are set at runtime via _configure_git_identity()
RUN printf '#!/bin/bash\necho "protocol=https"\necho "host=github.com"\necho "username=x-access-token"\necho "password=${GITHUB_TOKEN}"\n' \
      > /usr/local/bin/git-credential-repocraft \
    && chmod +x /usr/local/bin/git-credential-repocraft \
    && git config --global credential.helper repocraft \
    && git config --global --add safe.directory '*'

# Layer 8: Create non-root user for Claude Code
RUN useradd -m -s /bin/bash repocraft && \
    mkdir -p /repos /output /app && \
    chown -R repocraft:repocraft /repos /output /app

# Layer 9: Copy SDK runner script
COPY scripts/run_activity.py /app/run_activity.py
RUN chown repocraft:repocraft /app/run_activity.py

USER repocraft
WORKDIR /home/repocraft

# Ensure uv is available for non-root user
ENV PATH="/root/.local/bin:/home/repocraft/.local/bin:$PATH"

# Install Playwright Chromium as repocraft user (browsers stored in ~/.cache/ms-playwright)
RUN playwright install chromium

# Clear proxy for runtime
ENV http_proxy="" https_proxy="" all_proxy=""

WORKDIR /repos
CMD ["sleep", "infinity"]
