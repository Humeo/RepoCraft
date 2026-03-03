# M1 Implementation Complete

## What Was Built

M1 establishes the core end-to-end workflow for RepoCraft v2: `repocraft fix <issue_url>` now works from start to finish.

### New Files Created

1. **`src/repocraft/dispatcher.py`** (320 lines)
   - `async dispatch()` - orchestrates full activity lifecycle
   - Ensures container + repo ready
   - Auto-runs init for new repos
   - Constructs prompts based on activity kind (init/fix_issue/task/scan/respond_review/triage)
   - Executes `claude -p` with base64-encoded prompts
   - Streams output to SQLite logs table
   - Extracts exit code and summary
   - Updates activity status (pending → running → done|failed)

2. **`src/repocraft/templates/init_prompt.py`** (40 lines)
   - `get_init_prompt()` - returns prompt for repo exploration
   - Instructs agent to generate CLAUDE.md with: project overview, dev setup, testing, build, conventions, key files, gotchas

### Files Rewritten

3. **`src/repocraft/cli.py`** (240 lines)
   - Argparse with subcommands: `fix` (blocking), `submit/ask/daemon/status/logs` (stubs for M2)
   - `cmd_fix()` - parses issue URL, creates activity, dispatches with live log streaming
   - Rich UI with panels and live updates
   - Returns 0 on success, 1 on failure

4. **`src/repocraft/config.py`** (70 lines)
   - Simplified to env helpers: `get_anthropic_api_key()`, `get_github_token()`
   - URL parsing: `parse_issue_url()`, `parse_repo_url()`, `repo_id_from_url()`
   - Kept `RepoCraftConfig` dataclass for backward compat with tests

5. **`src/repocraft/container/manager.py`** (modified)
   - `exec_stream()` now yields `(line, None)` tuples, then `(None, exit_code)` sentinel
   - Uses `exec_inspect()` to get exit code after stream completes

6. **`tests/test_basic.py`** (30 lines)
   - Updated for v2 architecture
   - Removed evidence tests (v1 artifact)
   - Added `test_repo_id_from_url()` and `test_repo_id_from_url_with_git_suffix()`
   - All 6 tests pass

## How It Works

```bash
repocraft fix https://github.com/owner/repo/issues/42
```

1. **Parse & Validate**: Extract owner/repo/issue_number from URL
2. **Register Repo**: Add to SQLite if not exists
3. **Create Activity**: `kind="fix_issue"`, `trigger="issue:42"`, `status="pending"`
4. **Dispatch**:
   - Ensure container running (build image if needed, write user CLAUDE.md)
   - Clone repo if not present
   - Check for repo CLAUDE.md → if missing, run init activity first
   - Reset repo to clean state (`git reset --hard origin/main && git clean -fdx`)
   - Fetch issue from GitHub API
   - Build prompt with issue details
   - Execute `claude -p "$(echo <base64> | base64 -d)" --output-format stream-json --dangerously-skip-permissions --max-turns 200`
   - Stream output lines to SQLite logs table
   - Extract exit code from docker exec inspect
   - Update activity status based on exit code
5. **Display**: Live log streaming in terminal, final status panel (green=success, red=failure)

## Activity Kinds Supported

| Kind | Prompt Construction |
|------|---------------------|
| `init` | Explore repo, generate CLAUDE.md |
| `fix_issue` | Fetch issue from GitHub, build detailed prompt with title/body/comments, instruct to fix + test + PR |
| `task` | Free-form instruction from user |
| `scan` | Comprehensive audit (security, quality, deps, tests, docs) |
| `respond_review` | PR review comments → address each |
| `triage` | New issue → evaluate, reply, label |

## Key Design Decisions

1. **Base64 Prompt Encoding**: Avoids shell escaping issues with special characters in issue bodies
2. **Tuple Sentinel for Exit Code**: `exec_stream` yields `(line, None)` then `(None, exit_code)` - clean async pattern
3. **Auto-Init**: New repos automatically get init activity before first real task
4. **Stateless Dispatcher**: Each dispatch call is independent, no shared state
5. **Live Streaming**: CLI polls DB logs every 0.5s and displays in real-time

## Testing

```bash
uv run pytest -v
# 6 tests pass:
# - test_config_parses_owner_repo
# - test_config_invalid_url
# - test_parse_issue_url
# - test_parse_issue_url_invalid
# - test_repo_id_from_url
# - test_repo_id_from_url_with_git_suffix
```

## What's Next (M2)

- `scheduler.py` - background worker pool with asyncio.Semaphore + per-repo Lock
- `repocraft daemon` - foreground scheduler
- `repocraft submit <issue_url>` - async queue
- `repocraft ask <repo> "<instruction>"` - free-form task
- `repocraft status [target]` - check progress
- `repocraft logs <activity_id> [--follow]` - tail logs

## Commits

- `d80e97d` - M0: Docker image, templates, SQLite store, ContainerManager rewrite
- `678ff9f` - M1: dispatcher, CLI with fix subcommand, tests updated

## Repository

https://github.com/Humeo/RepoCraft
