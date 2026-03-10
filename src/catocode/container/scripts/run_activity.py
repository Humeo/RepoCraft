#!/usr/bin/env python3
"""SDK runner — executes inside the Docker container.

Usage:
    echo "prompt text" | python3 /app/run_activity.py <max_turns> <cwd> [session_id]

Reads prompt from stdin, runs Claude Agent SDK query(), streams structured
JSONL to stdout. The last line is always a result message.

Output format (one JSON object per line):
    {"type": "assistant", "text": "..."}
    {"type": "tool_use", "name": "Bash", "input": {"command": "..."}}
    {"type": "tool_result", "tool_use_id": "...", "output": "..."}
    {"type": "result", "result": "...", "is_error": false, "cost_usd": 1.23,
     "session_id": "abc", "num_turns": 5, "duration_ms": 12345}
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    query,
)


def _emit(obj: dict) -> None:
    """Write a JSON line to stdout and flush immediately."""
    print(json.dumps(obj, ensure_ascii=False, default=str), flush=True)


def _process_assistant_message(msg: AssistantMessage) -> None:
    """Extract and emit content blocks from an AssistantMessage."""
    for block in msg.content:
        if isinstance(block, TextBlock):
            _emit({"type": "assistant", "text": block.text})
        elif isinstance(block, ToolUseBlock):
            _emit({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })
        elif isinstance(block, ToolResultBlock):
            output = block.content if isinstance(block.content, str) else str(block.content)
            _emit({
                "type": "tool_result",
                "tool_use_id": block.tool_use_id,
                "output": output[:2000],  # truncate long outputs
                "is_error": block.is_error or False,
            })


def _process_result_message(msg: ResultMessage) -> None:
    """Emit the final result message."""
    _emit({
        "type": "result",
        "result": msg.result or "",
        "is_error": msg.is_error,
        "cost_usd": msg.total_cost_usd,
        "session_id": msg.session_id,
        "num_turns": msg.num_turns,
        "duration_ms": msg.duration_ms,
    })


async def run(prompt: str, max_turns: int, cwd: str, session_id: str | None = None) -> int:
    """Run SDK query and stream output. Returns 0 on success, 1 on error."""
    options = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        disallowed_tools=["AskUserQuestion", "EnterPlanMode"],
        max_turns=max_turns,
        cwd=Path(cwd),
    )

    if session_id:
        options.resume = session_id

    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                _process_assistant_message(msg)
            elif isinstance(msg, ResultMessage):
                _process_result_message(msg)
                return 1 if msg.is_error else 0
            elif isinstance(msg, SystemMessage):
                _emit({"type": "system", "subtype": msg.subtype})
    except Exception as e:
        _emit({
            "type": "result",
            "result": f"SDK error: {e}",
            "is_error": True,
            "cost_usd": None,
            "session_id": None,
            "num_turns": 0,
            "duration_ms": 0,
        })
        return 1

    return 0


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python3 run_activity.py <max_turns> <cwd> [session_id] [prompt_file]",
              file=sys.stderr)
        print("  Reads prompt from prompt_file if given, otherwise from stdin.",
              file=sys.stderr)
        sys.exit(2)

    max_turns = int(sys.argv[1])
    cwd = sys.argv[2]
    session_id = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] != "-" else None
    prompt_file = sys.argv[4] if len(sys.argv) > 4 else None

    if prompt_file:
        with open(prompt_file) as f:
            prompt = f.read()
    else:
        prompt = sys.stdin.read()

    if not prompt.strip():
        print("Error: empty prompt", file=sys.stderr)
        sys.exit(2)

    try:
        exit_code = asyncio.run(run(prompt, max_turns, cwd, session_id))
    except RuntimeError as exc:
        # anyio cancel-scope cleanup bug raises RuntimeError after the result is
        # already emitted. Treat as success if the text looks like the known issue.
        if "cancel scope" in str(exc).lower():
            exit_code = 0
        else:
            _emit({"type": "result", "result": f"RuntimeError: {exc}", "is_error": True,
                   "cost_usd": None, "session_id": None, "num_turns": 0, "duration_ms": 0})
            exit_code = 1
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
