from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.text import Text

from .agent.orchestrator import AgentOrchestrator
from .config import RepoCraftConfig
from .container.manager import ContainerManager
from .evidence.reporter import ReportGenerator
from .github.issue_fetcher import fetch_issue

console = Console()


def parse_issue_url(url: str) -> tuple[str, str, int]:
    match = re.match(
        r"https?://github\.com/([^/]+)/([^/]+)/issues/(\d+)",
        url.strip(),
    )
    if not match:
        raise ValueError(
            f"Invalid GitHub issue URL: {url!r}\n"
            "Expected format: https://github.com/owner/repo/issues/NUMBER"
        )
    return match.group(1), match.group(2), int(match.group(3))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repocraft",
        description="Reproduce, fix, and verify GitHub issues with an AI agent",
    )
    parser.add_argument(
        "issue_url",
        help="GitHub issue URL (e.g. https://github.com/owner/repo/issues/42)",
    )
    parser.add_argument(
        "--model",
        default="claude-sonnet-4-5-20251001",
        help="Claude model to use (default: claude-sonnet-4-5-20251001)",
    )
    parser.add_argument(
        "--max-budget",
        type=float,
        default=10.0,
        dest="max_budget",
        help="Maximum budget in USD per phase (default: 10.0)",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=30,
        dest="max_turns",
        help="Maximum agent turns per phase (default: 30)",
    )
    parser.add_argument(
        "--output-dir",
        default="output",
        dest="output_dir",
        help="Directory for output reports (default: output/)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    return parser


async def run_async(args: argparse.Namespace) -> int:
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    try:
        owner, repo, issue_number = parse_issue_url(args.issue_url)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        return 1

    repo_url = f"https://github.com/{owner}/{repo}"
    config = RepoCraftConfig(
        repo_url=repo_url,
        issue_number=issue_number,
        model=args.model,
        max_turns_per_phase=args.max_turns,
        max_budget_usd=args.max_budget,
        output_dir=Path(args.output_dir),
    )

    console.print(
        Panel(
            Text.from_markup(
                f"[bold]RepoCraft[/bold]\n"
                f"Repo: [cyan]{owner}/{repo}[/cyan]\n"
                f"Issue: [yellow]#{issue_number}[/yellow]\n"
                f"Model: [green]{config.model}[/green]\n"
                f"Budget: [magenta]${config.max_budget_usd:.2f}[/magenta] per phase"
            ),
            title="Starting",
            border_style="blue",
        )
    )

    # Fetch issue
    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as progress:
        task = progress.add_task("Fetching GitHub issue...", total=None)
        try:
            issue = await fetch_issue(
                owner=owner,
                repo=repo,
                issue_number=issue_number,
                github_token=config.github_token,
            )
            progress.update(task, description=f"[green]Fetched issue: {issue.title[:60]}")
        except Exception as e:
            console.print(f"[red]Failed to fetch issue:[/red] {e}")
            return 1

    console.print(f"\n[bold]Issue #{issue.number}:[/bold] {issue.title}\n")

    try:
        container_mgr = ContainerManager()
    except Exception as e:
        console.print(
            f"[red]Failed to connect to Docker:[/red] {e}\n"
            "[yellow]Is Docker Desktop running?[/yellow]"
        )
        return 1

    report = None

    try:
        # Build image and start container
        with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as progress:
            task = progress.add_task("Building Docker base image...", total=None)
            container_mgr.start(config.repo_url)
            progress.update(task, description="[green]Container started, repository cloned")

        def on_event(event: str, msg: str) -> None:
            if event == "phase_start":
                console.print(f"\n[bold blue]▶ {msg}[/bold blue]")
            elif event == "phase_end":
                color = "green" if "SUCCESS" in msg else "red"
                console.print(f"[{color}]✓ {msg}[/{color}]")
            elif event == "text" and args.verbose:
                console.print(f"[dim]{msg[:200]}[/dim]")

        orchestrator = AgentOrchestrator(
            container_mgr=container_mgr,
            model=config.model,
            max_turns_per_phase=config.max_turns_per_phase,
            max_budget_usd=config.max_budget_usd,
            on_event=on_event,
        )

        report = await orchestrator.run(issue)

    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user[/yellow]")
    except Exception as e:
        console.print(f"\n[red]Unexpected error:[/red] {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
    finally:
        container_mgr.stop()

    if report is None:
        return 1

    # Generate reports
    reporter = ReportGenerator(config.output_dir)
    paths = reporter.generate(report)

    status_color = "green" if report.overall_success else "red"
    status_text = "SUCCESS" if report.overall_success else "FAILED"
    console.print(
        Panel(
            Text.from_markup(
                f"[bold {status_color}]{status_text}[/bold {status_color}]\n"
                f"Total cost: [magenta]${report.total_cost_usd:.4f}[/magenta] USD\n\n"
                + "\n".join(f"  [cyan]{name}:[/cyan] {p}" for name, p in paths.items())
            ),
            title="Reports Generated",
            border_style=status_color,
        )
    )

    return 0 if report.overall_success else 1


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    sys.exit(asyncio.run(run_async(args)))


if __name__ == "__main__":
    main()
