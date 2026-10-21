import re
from pathlib import Path

_KEY = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")


def ticket_path(root: Path, project: str, key: str) -> Path:
    if not _KEY.match(key) or key.rsplit("-", 1)[0] != project:
        raise ValueError(f"invalid issue key: {key!r}")
    return root / "tickets" / project / f"{key}.md"


def _list(values: list[str]) -> str:
    return "[" + ", ".join(values) + "]"


def render_ticket(t: dict, site_url: str) -> str:
    lines = [
        "---",
        f"key: {t['key']}",
        f"url: {site_url}/browse/{t['key']}",
        f"type: {t.get('type')}",
        f"status: {t.get('status')}",
        f"priority: {t.get('priority')}",
        f"assignee: {t.get('assignee')}",
        f"reporter: {t.get('reporter')}",
        f"parent: {t.get('parent')}",
        f"labels: {_list(t.get('labels', []))}",
        f"components: {_list(t.get('components', []))}",
        f"created: {t.get('created')}",
        f"updated: {t.get('updated')}",
        "---",
        "",
        f"# {t['key']}: {t.get('summary')}",
        "",
        "## Description",
        "",
        t.get("description") or "_No description._",
        "",
    ]
    if t.get("links"):
        lines += ["## Links", ""]
        lines += [
            f"- {link['type']}: {link['key']} {link.get('summary') or ''}".rstrip()
            for link in t["links"]
        ]
        lines.append("")
    if t.get("history"):
        lines += ["## Status history", ""]
        lines += [f"- {h['date']}: {h['from']} -> {h['to']}" for h in t["history"]]
        lines.append("")
    if t.get("comments"):
        lines += ["## Comments", ""]
        for c in t["comments"]:
            lines += [f"**{c.get('author')}** ({c.get('created')}):", "", c.get("body", ""), ""]
    return "\n".join(lines).rstrip() + "\n"


def write_ticket(root: Path, t: dict, site_url: str) -> bool:
    """Write the ticket's markdown file; returns False when it already had this exact content."""
    path = ticket_path(root, t["project"], t["key"])
    text = render_ticket(t, site_url)
    try:
        if path.read_text() == text:
            return False
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return True


def remove_ticket(root: Path, project: str, key: str) -> None:
    ticket_path(root, project, key).unlink(missing_ok=True)
