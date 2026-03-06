from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .auth import get_auth
from .config import (
    get_anthropic_api_key,
    get_anthropic_base_url,
    get_patrol_config,
    parse_issue_url,
    repo_id_from_url,
)
from .container.manager import ContainerManager
from .dispatcher import dispatch
from .store import Store

console = Console()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="catocode",
        description="CatoCode — Self-Proving Autonomous Code Maintainer",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- watch ---
    watch_p = subparsers.add_parser("watch", help="Watch a repo (auto-triage issues, patrol)")
    watch_p.add_argument("repo_url", help="GitHub repo URL")

    # --- unwatch ---
    unwatch_p = subparsers.add_parser("unwatch", help="Stop watching a repo")
    unwatch_p.add_argument("repo_url", help="GitHub repo URL")

    # --- daemon ---
    daemon_p = subparsers.add_parser("daemon", help="Run background scheduler (blocking)")
    daemon_p.add_argument("--max-concurrent", type=int, default=3, help="Max concurrent activities")
    daemon_p.add_argument(
        "--webhook-port",
        type=int,
        default=0,
        metavar="PORT",
        help="Start webhook server on PORT (0 = disabled, default: 0)",
    )

    # --- fix ---
    fix_p = subparsers.add_parser("fix", help="Fix a GitHub issue (blocking)")
    fix_p.add_argument("issue_url", help="GitHub issue URL")
    fix_p.add_argument("--max-turns", type=int, default=200)

    # --- status ---
    status_p = subparsers.add_parser("status", help="Show repo or activity status")
    status_p.add_argument("target", nargs="?", help="repo_id or activity_id (omit for all)")

    # --- logs ---
    logs_p = subparsers.add_parser("logs", help="View activity logs")
    logs_p.add_argument("activity_id")
    logs_p.add_argument("--follow", "-f", action="store_true", help="Follow log output")

    return parser


# --- watch ---

async def cmd_watch(args: argparse.Namespace) -> int:
    repo_url = args.repo_url
    try:
        from .config import parse_repo_url
        owner, repo_name = parse_repo_url(repo_url)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        return 1

    repo_id = repo_id_from_url(repo_url)
    store = Store()
    container_mgr = ContainerManager()

    try:
        anthropic_api_key = get_anthropic_api_key()
        anthropic_base_url = get_anthropic_base_url()
        auth = get_auth()
        github_token = await auth.get_token()
    except RuntimeError as e:
        console.print(f"[red]Error:[/red] {e}")
        return 1

    # Check write access before registering
    from .github.permissions import check_repo_write_access
    console.print(f"[dim]Checking permissions for {owner}/{repo_name}...[/dim]")
    has_access, reason = await check_repo_write_access(owner, repo_name, github_token)
    if not has_access:
        console.print(f"[red]Permission denied:[/red] {reason}")
        console.print(
            "[dim]CatoCode needs write access to post comments and open PRs.[/dim]"
        )
        return 1
    console.print(f"[dim]✓ {reason}[/dim]")

    # Register and mark as watched
    store.add_repo(repo_id, repo_url)
    store.update_repo(repo_id, watch=1)

    # Initialize patrol budget
    patrol_cfg = get_patrol_config()
    store.init_patrol_budget(repo_id, patrol_cfg.max_issues, patrol_cfg.window_hours)

    console.print(f"[green]Watching[/green] {repo_url} (repo_id: {repo_id})")

    # Trigger init if not done yet
    result = None
    try:
        container_mgr.ensure_running(anthropic_api_key, github_token, anthropic_base_url)
        container_mgr.ensure_repo(repo_id, repo_url)
        result = container_mgr.exec(f"test -f /repos/{repo_id}/CLAUDE.md")
    except RuntimeError as e:
        console.print(f"[yellow]Container not available: {e}[/yellow]")
        console.print("[dim]Repo registered. Start daemon to initialize and begin watching.[/dim]")
        return 0

    if result and result.exit_code != 0:
        console.print(f"[dim]Queuing init activity for {repo_id}...[/dim]")
        activity_id = store.add_activity(repo_id, "init", "watch")
        console.print(f"[dim]Init activity {activity_id[:8]} queued. Start daemon to run.[/dim]")

    return 0


async def cmd_unwatch(args: argparse.Namespace) -> int:
    repo_id = repo_id_from_url(args.repo_url)
    store = Store()
    repo = store.get_repo(repo_id)
    if repo is None:
        console.print(f"[red]Repo not found:[/red] {repo_id}")
        return 1
    store.update_repo(repo_id, watch=0)
    console.print(f"[yellow]Unwatched[/yellow] {repo_id}")
    return 0


# --- daemon ---

async def cmd_daemon(args: argparse.Namespace) -> int:
    from .scheduler import Scheduler
    from .webhook.server import WebhookServer

    try:
        get_anthropic_api_key()
        auth = get_auth()
    except RuntimeError as e:
        console.print(f"[red]Error:[/red] {e}")
        return 1

    store = Store()
    container_mgr = ContainerManager()

    webhook_port: int = args.webhook_port
    info_lines = [
        "[bold green]CatoCode Daemon[/bold green]",
        f"Max concurrent: {args.max_concurrent}",
        f"Auth: {auth.auth_type()}",
    ]
    if webhook_port:
        info_lines.append(f"Webhook server: http://0.0.0.0:{webhook_port}")
    info_lines.append("Press Ctrl+C to stop")

    console.print(Panel("\n".join(info_lines), border_style="green"))

    scheduler = Scheduler(
        store=store,
        container_mgr=container_mgr,
        max_concurrent=args.max_concurrent,
        verbose=args.verbose,
        auth=auth,
    )

    tasks = [asyncio.create_task(scheduler.run())]

    if webhook_port:
        import uvicorn

        webhook_server = WebhookServer(store, auth=auth)
        config = uvicorn.Config(
            webhook_server.app,
            host="0.0.0.0",
            port=webhook_port,
            log_level="warning",
        )
        uv_server = uvicorn.Server(config)
        tasks.append(asyncio.create_task(uv_server.serve()))

    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        scheduler.stop()
        for t in tasks:
            t.cancel()

    console.print("[yellow]Daemon stopped[/yellow]")
    return 0


# --- fix ---

async def cmd_fix(args: argparse.Namespace) -> int:
    try:
        owner, repo, issue_number = parse_issue_url(args.issue_url)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        return 1

    repo_url = f"https://github.com/{owner}/{repo}"
    repo_id = repo_id_from_url(repo_url)

    console.print(
        Panel(
            f"[bold]CatoCode[/bold] — fix issue\n"
            f"Repo: [cyan]{owner}/{repo}[/cyan]\n"
            f"Issue: [yellow]#{issue_number}[/yellow]\n"
            f"Max turns: [magenta]{args.max_turns}[/magenta]",
            border_style="blue",
        )
    )

    try:
        anthropic_api_key = get_anthropic_api_key()
        anthropic_base_url = get_anthropic_base_url()
        auth = get_auth()
        github_token = await auth.get_token()
    except RuntimeError as e:
        console.print(f"[red]Error:[/red] {e}")
        return 1

    store = Store()
    container_mgr = ContainerManager()

    store.add_repo(repo_id, repo_url)
    activity_id = store.add_activity(repo_id, "fix_issue", f"issue:{issue_number}")
    console.print(f"[dim]Activity: {activity_id}[/dim]\n")

    try:
        dispatch_task = asyncio.create_task(
            dispatch(
                activity_id=activity_id,
                store=store,
                container_mgr=container_mgr,
                anthropic_api_key=anthropic_api_key,
                github_token=github_token,
                anthropic_base_url=anthropic_base_url,
                max_turns=args.max_turns,
                verbose=args.verbose,
            )
        )

        last_log_id = 0
        while not dispatch_task.done():
            logs = store.get_logs(activity_id)
            new_logs = [log for log in logs if log["id"] > last_log_id]
            for log in new_logs:
                _print_log_line(log["line"])
                last_log_id = log["id"]
            await asyncio.sleep(0.5)

        await dispatch_task

        # Print remaining logs
        logs = store.get_logs(activity_id)
        for log in logs:
            if log["id"] > last_log_id:
                _print_log_line(log["line"])

    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted[/yellow]")
        store.update_activity(activity_id, status="failed", summary="Interrupted by user")
        return 1
    except Exception as e:
        console.print(f"\n[red]Error:[/red] {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1

    activity = store.get_activity(activity_id)
    if activity is None:
        return 1

    if activity["status"] == "done":
        console.print(Panel(
            f"[bold green]SUCCESS[/bold green]\n\n{activity['summary'] or ''}",
            border_style="green",
        ))
        return 0
    else:
        console.print(Panel(
            f"[bold red]FAILED[/bold red]\n\n{activity['summary'] or ''}",
            border_style="red",
        ))
        return 1


# --- status ---

async def cmd_status(args: argparse.Namespace) -> int:
    store = Store()
    target = args.target

    if target is None:
        # Show all repos and recent activities
        repos = store.list_repos()
        if not repos:
            console.print("[dim]No repos registered. Run `catocode watch <url>` to start.[/dim]")
            return 0

        for repo in repos:
            watched = "👀" if repo["watch"] else "  "
            console.print(f"{watched} [cyan]{repo['id']}[/cyan] ({repo['repo_url']})")
            activities = store.list_activities(repo["id"])
            for a in list(activities)[-5:]:  # Last 5 per repo
                color = {"done": "green", "failed": "red", "running": "blue", "pending": "yellow"}.get(a["status"], "white")
                console.print(f"   [{color}]{a['status']:8}[/{color}] {a['id'][:8]} {a['kind']:15} {a['updated_at'][:19]}")
        return 0

    # Check if target is an activity_id or repo_id
    activity = store.get_activity(target)
    if activity:
        color = {"done": "green", "failed": "red", "running": "blue", "pending": "yellow"}.get(activity["status"], "white")
        console.print(f"Activity: {activity['id']}")
        console.print(f"  Repo:    {activity['repo_id']}")
        console.print(f"  Kind:    {activity['kind']}")
        console.print(f"  Status:  [{color}]{activity['status']}[/{color}]")
        console.print(f"  Session: {activity['session_id'] or 'none'}")
        console.print(f"  Summary: {activity['summary'] or 'none'}")
        return 0

    repo = store.get_repo(target)
    if repo:
        console.print(f"Repo: {repo['id']}")
        console.print(f"  URL:     {repo['repo_url']}")
        console.print(f"  Watch:   {'yes' if repo['watch'] else 'no'}")
        activities = store.list_activities(target)
        console.print(f"  Activities ({len(list(activities))}):")
        for a in list(activities)[-10:]:
            color = {"done": "green", "failed": "red", "running": "blue", "pending": "yellow"}.get(a["status"], "white")
            console.print(f"    [{color}]{a['status']:8}[/{color}] {a['id'][:8]} {a['kind']:15} {a['updated_at'][:19]}")
        return 0

    console.print(f"[red]Not found:[/red] {target}")
    return 1


# --- logs ---

async def cmd_logs(args: argparse.Namespace) -> int:
    store = Store()
    activity_id = args.activity_id

    # Support short IDs (first 8 chars)
    if len(activity_id) == 8:
        activities = store.list_activities()
        matches = [a for a in activities if a["id"].startswith(activity_id)]
        if len(matches) == 1:
            activity_id = matches[0]["id"]
        elif len(matches) > 1:
            console.print(f"[red]Ambiguous short ID:[/red] {activity_id}")
            return 1

    activity = store.get_activity(activity_id)
    if activity is None:
        console.print(f"[red]Activity not found:[/red] {activity_id}")
        return 1

    logs = store.get_logs(activity_id)
    for log in logs:
        _print_log_line(log["line"])

    if args.follow:
        last_id = logs[-1]["id"] if logs else 0
        while True:
            await asyncio.sleep(0.5)
            activity = store.get_activity(activity_id)
            if activity and activity["status"] in ("done", "failed"):
                # Print remaining logs then exit
                logs = store.get_logs(activity_id)
                for log in logs:
                    if log["id"] > last_id:
                        _print_log_line(log["line"])
                break
            logs = store.get_logs(activity_id)
            new_logs = [log for log in logs if log["id"] > last_id]
            for log in new_logs:
                _print_log_line(log["line"])
                last_id = log["id"]

    return 0


def _print_log_line(line: str) -> None:
    """Print a log line, stripping JSONL noise for readability."""
    import json
    stripped = line.strip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
            msg_type = obj.get("type", "")
            if msg_type == "assistant":
                text = obj.get("text", "").strip()
                if text:
                    console.print(f"[dim]💬 {text[:200]}[/dim]")
            elif msg_type == "tool_use":
                name = obj.get("name", "")
                inp = obj.get("input", {})
                cmd = inp.get("command", inp.get("path", str(inp)))[:100]
                console.print(f"[blue]🔧 {name}[/blue]: {cmd}")
            elif msg_type == "tool_result":
                out = obj.get("output", "")[:150]
                is_err = obj.get("is_error", False)
                color = "red" if is_err else "dim"
                console.print(f"[{color}]  → {out}[/{color}]")
            elif msg_type == "result":
                result = obj.get("result", "")[:300]
                cost = obj.get("cost_usd")
                turns = obj.get("num_turns")
                cost_str = f" (${cost:.4f}, {turns} turns)" if cost else ""
                console.print(f"[green]✅ Result{cost_str}:[/green] {result}")
            elif msg_type == "system":
                pass  # Skip system messages
            else:
                console.print(f"[dim]{stripped}[/dim]")
        except json.JSONDecodeError:
            console.print(f"[dim]{stripped}[/dim]")
    else:
        console.print(f"[dim]{stripped}[/dim]")


async def run_async(args: argparse.Namespace) -> int:
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")

    commands = {
        "watch": cmd_watch,
        "unwatch": cmd_unwatch,
        "daemon": cmd_daemon,
        "fix": cmd_fix,
        "status": cmd_status,
        "logs": cmd_logs,
    }

    handler = commands.get(args.command)
    if handler is None:
        console.print(f"[red]Unknown command:[/red] {args.command}")
        return 1

    return await handler(args)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(asyncio.run(run_async(args)))


if __name__ == "__main__":
    main()
