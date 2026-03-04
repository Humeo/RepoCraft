from __future__ import annotations

"""Activity-kind prompt templates.

Each prompt includes evidence collection instructions — the core of the
Proof of Work mechanism. Claude's CLAUDE.md defines the evidence *format*;
these prompts specify what evidence to collect for each activity kind.
"""


def patrol_prompt(repo_id: str, budget_remaining: int, last_areas: list[str] | None = None) -> str:
    """Prompt for proactive codebase scanning with rate-limiting awareness."""
    already_checked = ""
    if last_areas:
        already_checked = f"\n\nAreas recently checked (avoid duplicating): {', '.join(last_areas)}"

    return f"""\
You are performing a proactive security and quality audit of this codebase.

**Patrol Budget: {budget_remaining} issue(s) remaining this window.**
If budget is 0, output "Budget exhausted. Stopping patrol." and stop immediately.

## Audit Priorities (in order)
1. **Security vulnerabilities** — injection flaws, hardcoded credentials, insecure defaults
2. **Crash-level bugs** — null pointer dereferences, unhandled exceptions, resource leaks
3. **Logic errors** — incorrect calculations, off-by-one, race conditions
4. **Code quality** — dead code, deprecated dependencies with known CVEs
{already_checked}

## Process

For each potential issue you find:
1. **Reproduce it first** — run code, write a test, or demonstrate the failure
2. Only file an issue if you have concrete reproduction evidence
3. Do NOT file speculative issues ("this might be a problem")

## For Each Confirmed Bug, File a GitHub Issue

Use `gh issue create` with this format:
```
gh issue create --title "bug: <short description>" --body "..."
```

Issue body must include:
- Reproduction steps (exact commands)
- Evidence (paste the actual error output or failing test)
- Suggested fix direction

After filing, deduct 1 from your budget count.

When you've filed {budget_remaining} issue(s) OR exhausted the codebase, stop and summarize:
- How many issues found and filed
- Areas checked
- Recommended areas for next patrol
"""


def fix_issue_prompt(
    issue_number: int,
    issue_title: str,
    issue_body: str,
    repo_owner: str,
    repo_name: str,
) -> str:
    """Prompt for fixing a GitHub issue with before/after evidence."""
    return f"""\
Fix GitHub issue #{issue_number}: {issue_title}

## Issue Details

{issue_body}

## Your Process

### Step 1 — Understand
- Read the issue completely
- Identify the relevant code files
- Understand the root cause before touching anything

### Step 2 — Reproduce (Layer 1 Evidence)
- Run the application or tests to reproduce the failure
- Capture the exact error output: `command 2>&1 | tee /tmp/before.txt`
- For UI bugs: `npx playwright screenshot <url> /tmp/before.png`
- Save this evidence — you MUST include it in the PR

### Step 3 — Fix
- Write a minimal, targeted fix
- Do not refactor unrelated code
- Do not modify files unrelated to the bug

### Step 4 — Verify (Layer 2 Evidence)
- Run the same reproduction steps again
- Capture the success output: `command 2>&1 | tee /tmp/after.txt`
- For UI bugs: `npx playwright screenshot <url> /tmp/after.png`
- Run the full test suite: verify no regressions

### Step 5 — Create PR

Fork to your configured GitHub account, push the fix, create a PR:
```
gh pr create --title "fix: <short description> (#{issue_number})" --body "..."
```

PR body MUST include:
- `Fixes #{issue_number}`
- Before/After evidence section (paste actual test output)
- Summary table showing which tests passed before → after

Branch name: `repocraft/fix/{issue_number}-<short-slug>`

Repository: {repo_owner}/{repo_name}
"""


def triage_prompt(
    issue_number: int,
    issue_title: str,
    issue_body: str,
    issue_author: str,
) -> str:
    """Prompt for triaging a new GitHub issue."""
    return f"""\
Triage GitHub issue #{issue_number} submitted by @{issue_author}.

## Issue

**Title**: {issue_title}

**Body**:
{issue_body}

## Your Task

1. **Classify** the issue:
   - `bug`: Something that should work but doesn't
   - `feature`: New functionality request
   - `question`: User needs help or clarification
   - `duplicate`: Same as an existing issue (search with `gh issue list`)
   - `invalid`: Not a real issue (off-topic, spam, incomplete)

2. **Attempt quick reproduction** (for bug reports):
   - Try to reproduce the reported behavior
   - Note what you found (reproduced / not reproduced / need more info)

3. **Reply with a helpful comment** using `gh issue comment {issue_number} --body "..."`:
   - Acknowledge the report
   - Share reproduction findings (if you tried)
   - Ask for missing information if needed (steps to reproduce, version, OS)
   - For duplicates: link to the original issue
   - For features: describe rough feasibility

4. **Apply labels** if you have permission:
   - `gh issue edit {issue_number} --add-label "bug"` (or feature, question, duplicate, invalid)

Keep your reply concise and helpful. Don't promise timelines.
"""


def respond_review_prompt(pr_number: int, review_comments: str) -> str:
    """Prompt for responding to PR review comments."""
    return f"""\
Address review comments on PR #{pr_number}.

## Review Comments

{review_comments}

## Your Task

For each review comment:
1. Read it carefully
2. Either:
   a. **Fix the code**: Make the change, run tests to verify, commit
   b. **Reply explaining why not**: If the change is wrong/unnecessary, explain clearly

After addressing all comments:
- Push the updated commits to the existing PR branch (do NOT force-push)
- Reply to each review comment thread acknowledging what you did:
  ```
  gh pr review {pr_number} --comment --body "Addressed all review comments: ..."
  ```
- Include evidence that fixes work (test output, etc.)

Do NOT re-request review. The scheduler handles that.
"""


def review_pr_prompt(pr_number: int, pr_title: str, pr_diff: str) -> str:
    """Prompt for reviewing a pull request."""
    return f"""\
Review PR #{pr_number}: {pr_title}

## PR Diff

{pr_diff[:8000]}{"..." if len(pr_diff) > 8000 else ""}

## Your Review Process

1. **Understand the change** — what problem does this solve?
2. **Check correctness** — does the logic work? edge cases handled?
3. **Check tests** — are there tests? do they pass?
   - Run the test suite: capture output as evidence
4. **Check security** — any injection risks, credential leaks, auth bypasses?
5. **Check code quality** — readability, consistency with existing patterns

## Submit Review

Use `gh pr review {pr_number}` with structured feedback:

```
gh pr review {pr_number} --comment --body "..."
```

Or for approvals/request-changes:
```
gh pr review {pr_number} --approve --body "LGTM. Tests pass. ..."
gh pr review {pr_number} --request-changes --body "..."
```

Format your review body with:
- **Summary**: 1-2 sentence overall assessment
- **Test Results**: paste the test output
- **Comments**: specific issues with file:line references
- **Verdict**: Approve / Request Changes / Comment Only
"""
