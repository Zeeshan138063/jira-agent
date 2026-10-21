from pathlib import Path

TEMPLATES = {
    "Story": """## Context

_Who needs this and why now._

## User story

As a <role>, I want <capability> so that <benefit>.

## Acceptance criteria

- Given <state>, when <action>, then <result>.
- Given <state>, when <action>, then <result>.

## Scope

In scope:
- <item>

Out of scope:
- <item>

## Dependencies

- <ticket or team, or "none">
""",
    "Bug": """## Summary

_One sentence: what is broken and who it affects._

## Steps to reproduce

1. <step>
2. <step>

## Expected result

<what should happen>

## Actual result

<what happens instead, with error text or screenshots>

## Environment

<app version, platform, account type>

## Acceptance criteria

- Given <the repro steps>, when <fixed>, then <expected result>.
- <regression check>
""",
    "Task": """## Context

_Why this work is needed._

## What to do

- <step>

## Acceptance criteria

- <verifiable outcome>

## Scope

Out of scope:
- <item>

## Dependencies

- <ticket or "none">
""",
    "Spike": """## Question to answer

<the decision this research unblocks>

## Time box

<hours or days>

## Approach

- <what to try or read>

## Acceptance criteria

- A written recommendation with trade-offs is attached to this ticket.
- <follow-up tickets created, if any>
""",
    "Epic": """## Goal

<outcome in one or two sentences>

## Why now

<business or user driver>

## Success measures

- <metric or observable result>

## Scope

In scope:
- <area>

Out of scope:
- <area>

## Child work

- <stories to be created>
""",
}


def available_types() -> list[str]:
    return list(TEMPLATES)


def load_template(issue_type: str, name: str = "default", home: Path | None = None) -> dict:
    wanted = issue_type.strip().lower()
    canonical = next((t for t in TEMPLATES if t.lower() == wanted), "Task")
    if home and name != "default":
        custom = home / "templates" / name / f"{canonical}.md"
        if custom.is_file():
            return {"type": canonical, "source": str(custom), "body": custom.read_text()}
    return {"type": canonical, "source": "builtin", "body": TEMPLATES[canonical]}
