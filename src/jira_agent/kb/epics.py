from collections import Counter

from jira_agent.kb.cards import card, is_resolved, snippet
from jira_agent.kb.store import KbStore

GOAL_CHARS = 280


def _open_first_recent(children: list[dict]) -> list[dict]:
    recent = sorted(children, key=lambda t: t.get("updated") or "", reverse=True)
    return sorted(recent, key=is_resolved)


def epic_digest(
    kb: KbStore,
    key: str,
    max_children: int = 8,
    goal_chars: int = GOAL_CHARS,
    child_gist: int = 0,
    epic: dict | None = None,
) -> dict | None:
    """A short summary of an epic computed from its children, so it is never out of date."""
    epic = epic or kb.get(key)
    if epic is None:
        return None
    children = kb.children(key)
    by_status = Counter(c.get("status") for c in children)
    done = sum(1 for c in children if is_resolved(c))
    labels = Counter(label for c in children for label in c.get("labels", []))
    assignees = Counter(c.get("assignee") for c in children if c.get("assignee"))
    updated = max([c.get("updated") or "" for c in children] + [epic.get("updated") or ""])
    shown = _open_first_recent(children)[:max_children] if max_children else []
    return {
        "key": epic["key"],
        "title": epic.get("summary"),
        "status": epic.get("status"),
        "resolved": is_resolved(epic),
        "goal": snippet(epic.get("description"), goal_chars),
        "children": len(children),
        "done": done,
        "done_pct": round(100 * done / len(children)) if children else None,
        "by_status": dict(by_status.most_common(6)),
        "labels": [name for name, _ in labels.most_common(4)],
        "people": [name for name, _ in assignees.most_common(3)],
        "updated": updated or None,
        "child_cards": [card(c, 70, child_gist) for c in shown],
        "children_hidden": max(len(children) - len(shown), 0),
    }


def epic_cards(kb: KbStore, project: str | None, open_only: bool = False, limit: int = 30) -> list:
    rows = []
    for epic in kb.epics(project):
        if open_only and is_resolved(epic):
            continue
        children = kb.children(epic["key"])
        done = sum(1 for c in children if is_resolved(c))
        rows.append(
            {
                **card(epic, 80, 0),
                "children": len(children),
                "done": done,
                "updated": epic.get("updated"),
            }
        )
    rows.sort(key=lambda r: r["updated"] or "", reverse=True)
    return rows[:limit]
