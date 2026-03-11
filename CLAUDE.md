# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Development Commands

```bash
# Install dependencies (including dev tools)
uv sync --dev

# Run all tests
uv run pytest

# Run a single test
uv run pytest tests/test_basic.py::test_config_parses_owner_repo

# Run tests with coverage
uv run pytest --cov=src/catocode

# Run integration tests (requires Docker)
uv run pytest -m integration

# Lint (must pass before commit)
uv run ruff check src/
uv run ruff check src/ --fix   # auto-fix

# CLI mode: watch a repo + run daemon
uv run catocode watch https://github.com/owner/repo
uv run catocode daemon --webhook-port 8080

# SaaS mode: unified server (OAuth + API + webhooks + scheduler)
uv run catocode server --port 8000

# Frontend (Next.js)
cd frontend && bun install && bun dev
```

## Repository Layout

Two branches, two repos:

- **`main`** → `github.com/humeo/cato-code` (open source, Apache 2.0, no SaaS limits)
- **`dev`** → `github.com/humeo/cato-code-saas` (private, adds usage quota, billing, GCP deploy)

`dev` merges from `main`; never the reverse.

## Architecture Overview

CatoCode is an **autonomous GitHub code maintenance agent** with two operating modes:

1. **CLI mode** — Single-user, personal access token, `watch` + `daemon` commands
2. **SaaS mode** — Multi-tenant, GitHub App + OAuth, unified `server` command with Next.js frontend

```
┌─ Host Process ──────────────────────────────────────┐
│  CLI / FastAPI Server                                │
│  ├── Scheduler (3 async loops)                       │
│  ├── Webhook Server (per-repo + app-level)           │
│  ├── OAuth + REST API (SaaS mode)                    │
│  ├── Decision Engine                                 │
│  └── Store (SQLite or PostgreSQL)                    │
└──────────────────┬──────────────────────────────────┘
                   │ Docker API
┌─ Per-User Container ────────────────────────────────┐
│  catocode-worker[-{user_id}]                        │
│  ├── Claude Agent SDK (run_activity.py)              │
│  ├── Dev tools (git, gh, python, node, uv)           │
│  ├── /repos/{owner-repo}/ (cloned repos)             │
│  └── ~/.claude/ (skills, CLAUDE.md, memory)          │
└─────────────────────────────────────────────────────┘
```

## Core Execution Flow

1. **Event arrives** → GitHub webhook or CLI command
2. **Decision engine** (`decision/engine.py`) → classify event, pick skill
3. **Activity created** → stored in DB with `pending` status
4. **Scheduler** (`scheduler.py`) → picks pending activities, enforces per-user quota (SaaS), per-repo serial lock + global semaphore
5. **Dispatcher** (`dispatcher.py`) → ensures container running, builds prompt via `skill_renderer.py`, executes SDK runner (up to 3 retries)
6. **SDK runner** (`container/scripts/run_activity.py`) → runs inside Docker container with Claude Agent SDK
7. **Result** → comment/PR posted to GitHub, activity marked `done`

## Key Modules

### Orchestration

- `cli.py` — Entry point. Subcommands: `server`, `watch`, `unwatch`, `daemon`, `fix`, `status`, `logs`
- `scheduler.py` — Three async loops: approval check (30s), patrol scan (per-repo interval), dispatch (5s). In `_dispatch_one()`, per-user quota is checked before container resolution (SaaS mode only); exceeded activities are immediately marked `failed`
- `dispatcher.py` — Activity execution pipeline; activity status reflects quota block or container errors
- `skill_renderer.py` — Reads `SKILL.md` templates, strips YAML frontmatter, substitutes `{variable}` placeholders

### Data Layer

- `db.py` — Dual-backend abstraction: SQLite (`?` placeholders, WAL mode, thread lock) and PostgreSQL (`%s` placeholders, psycopg2). `connect(url)` factory returns normalized `Connection`
- `store.py` — All data operations. Schema defined in `SCHEMA` string; additive changes go in `_MIGRATIONS` list (idempotent — only "already exists" errors are silenced; unexpected errors are logged as warnings). Key tables: `users`, `sessions`, `oauth_states`, `install_states`, `repos`, `activities`, `logs`, `patrol_budget`, `issue_embeddings`

### Authentication (`auth/`)

- Factory pattern in `__init__.py`: prefers GitHub App (`GITHUB_APP_ID` + `GITHUB_APP_PRIVATE_KEY`), falls back to `GITHUB_TOKEN`
- `github_app.py` — Signs JWT, exchanges for 1-hour installation tokens, auto-refreshes before expiry
- `crypto.py` — Fernet encryption for stored GitHub access tokens; key derived via PBKDF2-SHA256 from `SESSION_SECRET_KEY` (not pad/truncate)

### API Layer (`api/`) — SaaS Mode

- `app.py` — FastAPI factory. CORS: only `FRONTEND_URL` in production (https); adds localhost in dev
- `oauth.py` — GitHub OAuth 2.0 login flow + session cookies (30-day, httpOnly). `install-callback` validates a CSRF state token from `install_states` table (not the raw user_id)
- `routes.py` — Protected endpoints behind `CurrentUser` session dependency. SSE log stream has 30-minute timeout. `/api/install-url` generates a random CSRF state stored in `install_states`
- `deps.py` — Session cookie validation, user extraction dependency
- `billing_webhook.py` — Receives normalized payment provider webhooks, upgrades/downgrades `subscription_tier` on the user record

### Webhook System (`webhook/`)

- `server.py` — Two endpoints: per-repo (`/webhook/github/{repo_id}`, optional secret) and app-level (`/webhook/app`, enforces `GITHUB_APP_WEBHOOK_SECRET`). Deduplication via delivery ID. Quota check before creating activities
- `parser.py` — Normalizes GitHub events into `WebhookEvent` with trigger format (`issue:123`, `pr:456`)
- `verifier.py` — HMAC-SHA256 signature verification

### Decision Engine (`decision/`)

- `engine.py` — Maps events to skill kinds: issue opened → `analyze_issue`, PR opened → `review_pr`, approval comment → `fix_issue`, @mention → `task`. Skips CatoCode's own PRs

### Container Management (`container/`)

- `manager.py` — Docker lifecycle: build image, create/start container, exec commands. Container named `catocode-worker-{user_id[:8]}` in SaaS mode
- `registry.py` — `ContainerRegistry`: lazy dict of `user_id → ContainerManager`
- `scripts/run_activity.py` — Executed inside container; runs Claude Agent SDK with prompt file

## Skills

Markdown prompt templates in `src/catocode/container/skills/`:

| Skill            | Trigger    | Purpose                                                             |
| ---------------- | ---------- | ------------------------------------------------------------------- |
| `analyze_issue`  | `issue:N`  | Analyze issue, suggest solutions, post comment, wait for `/approve` |
| `fix_issue`      | `issue:N`  | Execute approved fix with Proof of Work evidence                    |
| `review_pr`      | `pr:N`     | Review PR code quality, security, tests                             |
| `respond_review` | `pr:N`     | Address PR review feedback (session resume)                         |
| `triage`         | `issue:N`  | Classify and label issues                                           |
| `patrol`         | `budget:N` | Proactive codebase scan for bugs/security                           |

Each skill has `SKILL.md` (template with `{variable}` placeholders) and `README.md`.

## Approval Flow

1. New issue → `analyze_issue` posts analysis comment with "Reply `/approve` to proceed"
2. Admin replies `/approve` (or "go ahead", etc.)
3. Scheduler's approval loop detects comment → creates `fix_issue` activity
4. Fix executes with full Proof of Work (before/after evidence)

## Usage Quota (SaaS / dev branch only)

- Rolling 7-day window; free tier = 5 tasks, pro = 200
- `store.get_user_usage(user_id)` / `store.check_user_quota(user_id)` — checked in both webhook handler and `scheduler._dispatch_one()`
- `store.update_user_subscription(user_id, tier)` — called by billing webhook to upgrade/downgrade
- Frontend `UsageBar` component reads `/api/usage` or the `usage` field in `/api/me`

## Frontend

Next.js 15 + React 19 + TypeScript + Tailwind CSS in `frontend/`. Uses `bun` for package management.

Key pages: landing (`/`), GitHub App install (`/install`), dashboard (`/dashboard`), pricing (`/pricing`).

Frontend env vars (prefix `NEXT_PUBLIC_` for browser exposure):

- `NEXT_PUBLIC_API_URL` — backend base URL
- `NEXT_PUBLIC_GITHUB_APP_NAME` — used to build the GitHub App install link
- `NEXT_PUBLIC_BILLING_CHECKOUT_URL` — payment provider checkout (optional)

## Environment Variables

```bash
# Required (one auth method)
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_BASE_URL=...                  # Custom API endpoint (optional)
GITHUB_TOKEN=ghp_...                    # CLI mode (PAT)
# OR
GITHUB_APP_ID=...                       # SaaS mode (GitHub App)
GITHUB_APP_PRIVATE_KEY=...              # RSA private key (newlines as \n)
GITHUB_APP_INSTALLATION_ID=...

# SaaS mode additional
GITHUB_OAUTH_CLIENT_ID=...
GITHUB_OAUTH_CLIENT_SECRET=...
SESSION_SECRET_KEY=...                  # High-entropy string; PBKDF2-derived into Fernet key
CATOCODE_BASE_URL=https://...           # Determines production mode (https → strict CORS, secure cookies)
FRONTEND_URL=https://...
GITHUB_APP_NAME=catocode-bot
GITHUB_APP_WEBHOOK_SECRET=...          # Required in production; enforced on /webhook/app

# Optional
CATOCODE_MEM=8g
CATOCODE_CPUS=4
CATOCODE_PATROL_MAX_ISSUES=5
CATOCODE_PATROL_WINDOW_HOURS=12
DATABASE_URL=postgresql://...           # Default: SQLite at data/catocode.db
BILLING_WEBHOOK_SECRET=...             # HMAC secret for payment provider webhooks
```

## SaaS vs CLI Mode Detection

Auto-detected at startup in `cli.py`:

- **SaaS mode**: `GITHUB_OAUTH_CLIENT_ID` + `SESSION_SECRET_KEY` set → unified FastAPI app
- **CLI mode**: Otherwise → webhook-only server

In SaaS mode, repos and activities are scoped to `user_id`; containers are per-user.
