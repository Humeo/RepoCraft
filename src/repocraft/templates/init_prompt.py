from __future__ import annotations


def get_init_prompt() -> str:
    """Return the prompt for exploring a new repo and generating CLAUDE.md."""
    return """\
Explore this repository thoroughly and understand its structure, architecture, testing approach,
build process, code conventions, and — critically — how to reproduce bugs and take screenshots.

Your task is to create a `CLAUDE.md` file in the project root directory. This file will be used
by all future activities (fix_issue, patrol, review, etc.) to work effectively with this codebase.

## Required Sections

### Project Overview
- What this project does (1-2 sentences)
- Tech stack: languages, frameworks, key libraries
- Architecture overview (monolith, microservices, API server, CLI, etc.)

### Development Setup
- How to install dependencies (exact commands)
- Environment variables or config files needed
- How to run the project locally (dev server, port, etc.)
- Any required external services (database, Redis, etc.) and how to start them

### Testing
- Test framework used (pytest, jest, vitest, go test, etc.)
- How to run all tests (exact command)
- How to run a single test file or test case
- How to run tests with verbose/debug output
- Test file locations and naming conventions
- How to set up test fixtures or seed data

### Bug Reproduction Guide
This section is critical for the Proof of Work mechanism.
- How to reproduce the most common types of bugs in this project
- For web projects: how to start the dev server, what URL to visit, demo credentials if any
- For CLI tools: example invocations that exercise common code paths
- For libraries: how to write a minimal reproduction script
- For API servers: example curl/httpx commands for key endpoints
- How to capture logs: log file locations, how to enable debug logging
- How to check database state (if applicable): connection commands, key tables

### Screenshots (if web project)
Only include this section if the project has a web UI.
- How to start the dev server and which port it runs on
- Playwright command to take a screenshot:
  `npx playwright screenshot http://localhost:PORT/path /tmp/screenshot.png`
- Any authentication needed (login steps before screenshots)
- Key pages/URLs to screenshot for evidence

### Building & Deployment
- Build commands (if applicable)
- Output artifacts and their locations

### Code Conventions
- Linting/formatting tools and commands (eslint, prettier, black, ruff, etc.)
- How to run the linter before committing
- Import order conventions (if enforced)
- Naming conventions
- File organization patterns

### Key Files & Directories
- Entry points (main.py, index.ts, main.go, etc.)
- Configuration files
- Important modules or components
- Where business logic lives vs infrastructure code

### Gotchas & Special Notes
- Known issues or workarounds
- Performance considerations
- Security considerations (auth, rate limiting, secrets handling)
- Anything unusual about this codebase

## Instructions

1. Explore the repo systematically: read README, package.json/pyproject.toml/go.mod, CI config
2. Try to actually run the tests to confirm the test commands work
3. If there's a web UI, try starting the dev server briefly to confirm the port
4. Write CLAUDE.md to the project root with your findings
5. Use clear, concise, actionable language — future activities will rely on this
"""
