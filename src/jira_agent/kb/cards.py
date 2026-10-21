import re

_BULLET = re.compile(r"^[-*]\s+")
_SPACES = re.compile(r"\s+")
LABEL_MAX = 40


def snippet(text: str | None, limit: int) -> str:
    """The first readable words of a description: headings and ALL-CAPS section labels dropped."""
    if limit <= 0:
        return ""
    kept = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.isupper() and len(line) <= LABEL_MAX:
            continue
        kept.append(_BULLET.sub("", line))
    flat = _SPACES.sub(" ", " ".join(kept)).strip()
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def clip(text: str | None, limit: int) -> str:
    flat = _SPACES.sub(" ", text or "").strip()
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def is_resolved(t: dict) -> bool:
    """Finished work: Jira's resolution is set, or its status sits in the 'done' category.

    Some workflows never set a resolution, so the status category matters.
    """
    return bool(t.get("resolution")) or t.get("status_category") == "done"


def card(t: dict, summary_chars: int = 90, gist_chars: int = 140) -> dict:
    """A compact view of a ticket: enough to recognise it, a fraction of the full text."""
    out = {
        "key": t["key"],
        "type": t.get("type"),
        "status": t.get("status"),
        "resolved": is_resolved(t),
        "summary": clip(t.get("summary"), summary_chars),
    }
    if t.get("parent"):
        out["epic"] = t["parent"]
    if t.get("labels"):
        out["labels"] = t["labels"][:4]
    if t.get("assignee"):
        out["assignee"] = t["assignee"]
    gist = snippet(t.get("description"), gist_chars)
    if gist:
        out["gist"] = gist
    return out


def card_line(c: dict) -> str:
    done = " done" if c.get("resolved") else ""
    epic = f" [{c['epic']}]" if c.get("epic") else ""
    gist = f" - {c['gist']}" if c.get("gist") else ""
    return f"{c['key']} {c.get('type')}/{c.get('status')}{done}{epic}: {c['summary']}{gist}"
