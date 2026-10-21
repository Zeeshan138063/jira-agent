"""Token meter: how much the CLI hands back to the model, per command.

Only the command name (never its arguments), sizes and timings are recorded. Tokens are estimated
as characters / 4, the same rule `kb context` uses for its budget; real model tokens differ a
little.
"""

import json
import math
import os
import time
from collections import Counter, defaultdict
from pathlib import Path

from jira_agent.config import default_home, private_dir

LOG_NAME = "usage.jsonl"
TRIM_BYTES = 3_000_000
KEEP_LINES = 10_000
CHARS_PER_TOKEN = 4


def estimate_tokens(chars: int) -> int:
    return math.ceil(chars / CHARS_PER_TOKEN)


def command_name(argv: list[str]) -> str:
    """The command words only, e.g. 'kb context' or 'jira get' (no arguments, no values)."""
    words = []
    for token in argv:
        if token.startswith("-") or len(words) == 2:
            break
        words.append(token)
    return " ".join(words) or "(none)"


class CountingStream:
    """Passes writes through to a stream while counting the characters."""

    def __init__(self, stream):
        self.stream = stream
        self.chars = 0

    def write(self, text: str) -> int:
        self.chars += len(text)
        return self.stream.write(text)

    def flush(self) -> None:
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


def log_path(home: Path | None = None) -> Path:
    return (home or default_home()) / LOG_NAME


def record(entry: dict, home: Path | None = None) -> None:
    """Append one measurement; never raises, because metering must not break a command."""
    if os.environ.get("JIRA_AGENT_NO_USAGE"):
        return
    try:
        path = log_path(home)
        private_dir(path.parent)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        if path.stat().st_size > TRIM_BYTES:
            kept = path.read_text(encoding="utf-8").splitlines()[-KEEP_LINES:]
            path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    except OSError:
        pass


def load_entries(home: Path | None = None, days: int | None = None) -> list[dict]:
    path = log_path(home)
    if not path.exists():
        return []
    cutoff = time.time() - days * 86400 if days else 0
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("ts", 0) >= cutoff:
            entries.append(entry)
    return entries


def _percentile(values: list[int], share: float) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(share * len(ordered)))] if ordered else 0


def summarize(entries: list[dict]) -> dict:
    by_command: dict[str, list[int]] = defaultdict(list)
    seconds: dict[str, float] = defaultdict(float)
    by_day: Counter = Counter()
    for e in entries:
        by_command[e["command"]].append(e["tokens"])
        seconds[e["command"]] += e.get("seconds", 0)
        by_day[time.strftime("%Y-%m-%d", time.localtime(e["ts"]))] += e["tokens"]
    commands = [
        {
            "command": name,
            "calls": len(tokens),
            "tokens": sum(tokens),
            "avg": round(sum(tokens) / len(tokens)),
            "p90": _percentile(tokens, 0.9),
            "max": max(tokens),
            "seconds": round(seconds[name], 1),
        }
        for name, tokens in by_command.items()
    ]
    commands.sort(key=lambda c: c["tokens"], reverse=True)
    heaviest = sorted(entries, key=lambda e: e["tokens"], reverse=True)[:5]
    total = sum(c["tokens"] for c in commands)
    return {
        "calls": len(entries),
        "tokens": total,
        "avg_per_call": round(total / len(entries)) if entries else 0,
        "by_command": commands,
        "by_day": dict(sorted(by_day.items())),
        "heaviest_calls": [
            {
                "command": e["command"],
                "tokens": e["tokens"],
                "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(e["ts"])),
            }
            for e in heaviest
        ],
    }


def static_costs(plugin_root: Path) -> dict:
    """Tokens the instructions themselves cost when the model loads them (once per session)."""

    def size(path: Path) -> int:
        return estimate_tokens(len(path.read_text(encoding="utf-8"))) if path.exists() else 0

    agent = plugin_root / "agents" / "jira-agent.md"
    skills = {p.parent.name: size(p) for p in sorted((plugin_root / "skills").glob("*/SKILL.md"))}
    return {
        "agent": size(agent),
        "skills": skills,
        "all_skills": sum(skills.values()),
        "note": "the agent file loads at session start; a skill loads only when it is used",
    }


def render_table(summary: dict, static: dict | None = None) -> str:
    if not summary["calls"]:
        return "No usage recorded yet. Run some jira-agent commands, then try again."
    rows = summary["by_command"]
    width = max(len(r["command"]) for r in rows + [{"command": "command"}])
    lines = [
        f"Tokens handed to the model by the CLI (estimated, characters / {CHARS_PER_TOKEN})",
        "",
        f"{'command'.ljust(width)}  {'calls':>5}  {'total':>8}  {'avg':>6}  {'p90':>6}  {'max':>6}",
    ]
    for r in rows:
        lines.append(
            f"{r['command'].ljust(width)}  {r['calls']:>5}  {r['tokens']:>8}  "
            f"{r['avg']:>6}  {r['p90']:>6}  {r['max']:>6}"
        )
    lines += [
        "",
        f"{summary['calls']} calls, {summary['tokens']} tokens, "
        f"{summary['avg_per_call']} per call on average",
    ]
    if static:
        lines += [
            "",
            f"Instructions loaded by the model: agent {static['agent']} tokens; skills "
            f"{static['all_skills']} tokens in total, loaded one at a time as needed",
        ]
        lines += [f"  {name}: {tokens}" for name, tokens in static["skills"].items()]
    return "\n".join(lines)
