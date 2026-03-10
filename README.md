# 🏛️ CatoCode

**The Autonomous GitHub Code Maintenance Agent**

> Named after Cato the Elder, the Roman statesman renowned for his unwavering integrity. CatoCode never compromises—every bug fix comes with proof, every claim backed by evidence.

[![CI](https://github.com/yourusername/catocode/actions/workflows/ci.yml/badge.svg)](https://github.com/yourusername/catocode/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.12+-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)
[![Docker](https://img.shields.io/badge/docker-required-blue)](https://www.docker.com/)

---

## 🚀 Quick Start (Self-Hosted)

```bash
git clone https://github.com/yourusername/catocode.git
cd catocode
cp .env.example .env   # Fill in your API keys
docker compose up -d
```

That's it. CatoCode is now watching your repos and will fix issues autonomously.

---

## 🎯 What Is CatoCode?

CatoCode is an **autonomous agent** that monitors your GitHub repositories and:

- **Fixes bugs** — reproduces them first, then patches, then verifies
- **Reviews PRs** — catches quality issues before merge
- **Triages issues** — classifies, labels, and responds automatically
- **Patrols code** — proactively scans for security issues and bugs

Every action includes **Proof of Work**: before/after evidence so you can verify results in 30 seconds without manual testing.

---

## 🔧 Proof of Work

Every PR CatoCode creates includes an evidence table:

```markdown
| Check            | Before        | After         |
|------------------|---------------|---------------|
| Failing test     | ❌ FAIL       | ✅ PASS       |
| Full test suite  | 41 passed, 1 failed | 42 passed |
| Related tests    | Broken        | Working       |
```

No trust required — just look at the proof.

---

## 📦 Self-Hosted Setup

### Prerequisites

- Docker + Docker Compose
- GitHub account (personal or organization)
- Anthropic API key — [get one here](https://console.anthropic.com/)

### Configuration

Copy `.env.example` and fill in your credentials:

```bash
cp .env.example .env
```

```bash
# Required
ANTHROPIC_API_KEY=sk-ant-...

# Choose one auth method:
# Option A — Personal Access Token (simplest)
GITHUB_TOKEN=ghp_...

# Option B — GitHub App (recommended for organizations)
GITHUB_APP_ID=...
GITHUB_APP_PRIVATE_KEY=...           # RSA private key (newlines as \n)
GITHUB_APP_INSTALLATION_ID=...
GITHUB_OAUTH_CLIENT_ID=...
GITHUB_OAUTH_CLIENT_SECRET=...
SESSION_SECRET_KEY=...               # 32+ random bytes (openssl rand -hex 32)
CATOCODE_BASE_URL=http://localhost:8000
FRONTEND_URL=http://localhost:3000
```

### Start

```bash
docker compose up -d
```

Dashboard available at `http://localhost:3000` (GitHub App mode) or use the CLI directly.

### CLI Mode

```bash
# Install
uv sync

# Watch a repo
uv run catocode watch https://github.com/owner/repo

# Start the daemon
uv run catocode daemon

# Fix a specific issue right now
uv run catocode fix https://github.com/owner/repo/issues/42

# Check status
uv run catocode status
```

---

## 🏗️ Architecture

```
┌─ Host Process ──────────────────────────────────────┐
│  CLI / FastAPI Server                                │
│  ├── Scheduler (approval check, patrol, dispatch)   │
│  ├── Webhook Server (per-repo + app-level)          │
│  ├── OAuth + REST API (GitHub App mode)             │
│  └── Store (SQLite or PostgreSQL)                   │
└──────────────────┬──────────────────────────────────┘
                   │ Docker API
┌─ Per-User Container ────────────────────────────────┐
│  catocode-worker                                    │
│  ├── Claude Agent SDK                               │
│  ├── Dev tools (git, gh, python, node, uv)          │
│  └── /repos/{owner-repo}/ (cloned repos)            │
└─────────────────────────────────────────────────────┘
```

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

## 🧪 Development

```bash
# Install dependencies
uv sync

# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov=src/catocode

# Run integration tests (requires Docker)
uv run pytest -m integration

# Start the server (GitHub App mode)
uv run catocode server --port 8000

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

## 📚 Documentation

- [Skill Architecture](docs/SKILL_ARCHITECTURE.md) — How the skill system works
- [Skill Implementation](docs/SKILL_IMPLEMENTATION_SUMMARY.md) — Technical details
- [Skills Reference](src/catocode/container/skills/) — All available skills

---

## 🔒 Security

- Your code never leaves your infrastructure (except the Anthropic API)
- GitHub tokens stored locally, encrypted at rest in GitHub App mode
- CatoCode runs in an isolated Docker container
- All commits signed as `CatoCode <catocode@catocode.dev>`

---

## 📄 License

Apache License 2.0 — see [LICENSE](LICENSE) for details.

---

<div align="center">

**"Integrity is doing the right thing, even when no one is watching."**
— Cato the Elder

[Get Started](#-quick-start-self-hosted) • [Documentation](docs/) • [Contributing](#-contributing)

</div>
