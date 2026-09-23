"""Shared helper for running the CLI as a subprocess from the web layer."""

from __future__ import annotations

import asyncio


async def run_cli(args: list[str], timeout: float = 1800) -> tuple[int, str]:
    """Run the CLI in a fresh process so UI actions never block the event loop."""
    proc = await asyncio.create_subprocess_exec(
        "discord-recall",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, f"timed out after {int(timeout)}s"
    return proc.returncode or 0, out.decode(errors="replace")


def last_line(text: str) -> str:
    """Last meaningful line of CLI output, with the loguru prefix stripped."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return "(no output)"
    line = lines[-1].strip()
    if " - " in line and "|" in line:
        line = line.split(" - ", 1)[1].strip()
    return line
