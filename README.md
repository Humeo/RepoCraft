# 🏛️ CatoCode

**The Autonomous GitHub Code Maintenance Agent**

> Named after Cato the Elder, the Roman statesman renowned for his unwavering integrity. CatoCode never compromises—every bug fix comes with proof, every claim backed by evidence.

[![CI](https://github.com/humeo/cato-code/actions/workflows/ci.yml/badge.svg)](https://github.com/humeo/cato-code/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.12+-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)
[![Docker](https://img.shields.io/badge/docker-required-blue)](https://www.docker.com/)

---

## 🎯 What Is CatoCode?

CatoCode is an **autonomous agent** that monitors your GitHub repositories and:

- **Fixes bugs** — reproduces them first, then patches, then verifies
- **Reviews PRs** — catches quality issues before merge
- **Triages issues** — classifies, labels, and responds automatically
- **Patrols code** — proactively scans for security issues and bugs

Every action includes **Proof of Work**: before/after evidence so you can verify results in 30 seconds without manual testing.

```markdown
| Check            | Before               | After       |
|------------------|----------------------|-------------|
| Failing test     | ❌ FAIL              | ✅ PASS     |
| Full test suite  | 41 passed, 1 failed  | 42 passed   |
```

---

## 🚀 Quick Start (CLI Mode — Open Source)

The simplest way to run CatoCode: point it at a GitHub repo, start the daemon, and it will handle issues automatically.

### 1. Prerequisites

- Python 3.12+
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or Docker Engine)
- [uv](https://docs.astral.sh/uv/getting-started/installation/) — Python package manager
- Anthropic API key — [get one here](https://console.anthropic.com/)
- GitHub Personal Access Token with `repo` + `issues` + `pull_requests` scopes

### 2. Install

```bash
git clone https://github.com/humeo/cato-code.git
cd cato-code
uv sync
```

### 3. Configure

Create a `.env` file in the project root:

```bash
# Required
ANTHROPIC_API_KEY=sk-ant-...
GITHUB_TOKEN=ghp_...          # Personal Access Token

# Optional
GIT_USER_NAME=CatoCode
GIT_USER_EMAIL=catocode@bot.local
CATOCODE_PATROL_MAX_ISSUES=5
CATOCODE_PATROL_WINDOW_HOURS=12
```

### 4. Watch a Repository

```bash
uv run catocode watch https://github.com/owner/repo
```

This registers the repo in the local database (`~/.catocode/catocode.db`).

### 5. Start the Daemon

```bash
uv run catocode daemon --webhook-port 8080
```

The daemon runs three background loops:
- **Dispatch** (every 5s) — picks up pending activities and runs them in Docker
- **Approval check** (every 30s) — detects `/approve` comments and triggers fixes
- **Patrol** (configurable) — proactively scans repos for bugs

### 6. Expose Webhooks (Required for Real-Time Events)

For CatoCode to react to new issues and PRs in real time, GitHub needs to reach the daemon's webhook endpoint. Use [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/get-started/create-local-tunnel/) for a free public URL:

```bash
# Install cloudflared
brew install cloudflare/cloudflare/cloudflared   # macOS

# Create a temporary public tunnel
cloudflared tunnel --url http://localhost:8080
# Output: https://your-tunnel-id.trycloudflare.com
```

Then add a webhook in your GitHub repo:
- **URL**: `https://your-tunnel-id.trycloudflare.com/webhook/github/{owner-repo}`
  - Replace `{owner-repo}` with e.g. `alice-myproject` (owner and repo joined by `-`)
- **Content type**: `application/json`
- **Events**: Issues, Issue comments, Pull requests, Pull request reviews

### 7. (Optional) Run the Dashboard

A read-only Next.js dashboard lets you view watched repos, activity history, and logs.

```bash
cd frontend
bun install
bun dev
# Open http://localhost:3000
```

> **Note**: In CLI mode, the dashboard is read-only — no GitHub login required. Authentication is only needed in GitHub App / SaaS mode.

---

## 🔄 How It Works

1. **New issue opened** on GitHub → webhook fires to CatoCode
2. **Daemon receives** the event → `analyze_issue` activity created
3. **Docker worker container** spins up (built automatically on first run)
4. **Claude Agent** (via [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)) analyzes the issue, posts a comment with analysis + proposed solutions
5. **You reply** `/approve` on the GitHub issue
6. **CatoCode** creates a PR with the fix + Proof of Work evidence

---

## 📋 CLI Reference

```bash
# Watch a repo (registers in local DB)
uv run catocode watch https://github.com/owner/repo

# Stop watching a repo
uv run catocode unwatch https://github.com/owner/repo

# Start the daemon (webhook server + scheduler)
uv run catocode daemon --webhook-port 8080

# Fix a specific issue immediately (no webhook needed)
uv run catocode fix https://github.com/owner/repo/issues/42

# Show watched repos and recent activity
uv run catocode status

# Tail recent logs
uv run catocode logs
```

---

## 🏗️ Architecture

```
┌─ Host Process ──────────────────────────────────────┐
│  CLI Daemon                                          │
│  ├── Scheduler (approval check, patrol, dispatch)   │
│  ├── Webhook Server (/webhook/github/{repo_id})      │
│  └── Store (SQLite at ~/.catocode/catocode.db)       │
└──────────────────┬──────────────────────────────────┘
                   │ Docker API
┌─ Worker Container ──────────────────────────────────┐
│  catocode-worker                                    │
│  ├── Claude Agent SDK + Claude Code CLI             │
│  ├── Dev tools (git, gh, python, node, uv)          │
│  └── /repos/{owner-repo}/ (cloned repos)            │
└─────────────────────────────────────────────────────┘
```

The Docker image is built automatically on first run (~5–10 minutes). Subsequent starts reuse the cached image.

### Skills

CatoCode uses Markdown prompt templates called **skills**:

| Skill | Trigger | What It Does |
|-------|---------|--------------|
| `analyze_issue` | Issue opened | Analyzes issue, posts plan, waits for `/approve` |
| `fix_issue` | After `/approve` | Reproduces → patches → verifies → creates PR |
| `review_pr` | PR opened | Reviews code quality, security, tests |
| `respond_review` | PR review comments | Addresses feedback, pushes updates |
| `triage` | Issue opened | Classifies and labels issues |
| `patrol` | Scheduled | Proactive scan for bugs/security issues |

Skills live in `src/catocode/container/skills/` and can be customized without code changes.

---

## ⚙️ Full Configuration Reference

```bash
# Anthropic
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_BASE_URL=...              # Optional: custom API endpoint

# GitHub Auth — choose one:
GITHUB_TOKEN=ghp_...               # CLI mode: Personal Access Token

# Git identity (used for commits inside the container)
GIT_USER_NAME=CatoCode
GIT_USER_EMAIL=catocode@bot.local

# Webhook server
WEBHOOK_PORT=8080

# Patrol limits
CATOCODE_PATROL_MAX_ISSUES=5       # Max issues to patrol per window
CATOCODE_PATROL_WINDOW_HOURS=12    # Rolling window for patrol

# Container resources
CATOCODE_MEM=8g                    # Memory limit for worker container
CATOCODE_CPUS=4                    # CPU limit

# Database (default: SQLite)
DATABASE_URL=postgresql://...       # Optional: use PostgreSQL instead
```

---

## 🐳 Docker Compose (Alternative)

For a fully containerized setup:

```bash
cp .env.example .env
# Edit .env with your credentials
docker compose up -d
```

Dashboard at `http://localhost:3000`, webhook server at `http://localhost:8080`.

---

## 🔐 GitHub App Mode (Advanced)

For teams and organizations, GitHub App mode offers:
- Automatic installation across all repos in an org
- OAuth dashboard with per-user activity tracking
- No need to manually configure webhooks per repo

See [docs/GITHUB_APP_SETUP.md](docs/GITHUB_APP_SETUP.md) for setup instructions.

---

## 🧪 Development

```bash
# Install dependencies (including dev tools)
uv sync --dev

# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov=src/catocode

# Run integration tests (requires Docker)
uv run pytest -m integration

# Lint
uv run ruff check src/
uv run ruff check src/ --fix

# Frontend
cd frontend && bun install && bun dev
```

---

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/amazing-feature`
3. Make your changes and add tests
4. Run tests: `uv run pytest`
5. Commit: `git commit -m "feat: add amazing feature"`
6. Open a PR

---

## 🔒 Security

- Your code never leaves your infrastructure (except the Anthropic API)
- GitHub token stored locally in `.env` — never committed
- CatoCode runs in an isolated Docker container with limited permissions
- All commits are attributed to the configured `GIT_USER_NAME` / `GIT_USER_EMAIL`

---

## 📄 License

Apache License 2.0 — see [LICENSE](LICENSE) for details.

---

<div align="center">

**"Integrity is doing the right thing, even when no one is watching."**
— Cato the Elder

[Get Started](#-quick-start-cli-mode--open-source) • [CLI Reference](#-cli-reference) • [Contributing](#-contributing)

</div>
