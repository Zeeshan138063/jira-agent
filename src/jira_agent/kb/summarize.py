import json
import re
from collections import Counter
from pathlib import Path

from jira_agent.groom import checklist
from jira_agent.kb.store import KbStore

STOPWORDS = frozenset(
    "the a an and or of to in on for with from by at as is are be this that it its if when then "
    "not no add fix update make new use using via into out up we can should will".split()
)
_AC = re.compile(r"acceptance criteria|\bgiven\b.*\bwhen\b.*\bthen\b", re.I | re.S)


def _top(counter: Counter, n: int = 10) -> list[tuple[str, int]]:
    return [(k, v) for k, v in counter.most_common(n) if k]


STATS_VERSION = 2


def _pct(items: list[dict], test) -> int:
    return round(100 * sum(1 for t in items if test(t.get("description") or "")) / len(items))


def _habits_of(items: list[dict]) -> dict:
    return {
        "n": len(items),
        "description": _pct(items, lambda d: len(d.strip()) >= 150),
        "acceptance": _pct(items, checklist.has_acceptance),
        "scope": _pct(items, checklist.has_scope),
        "repro": _pct(items, lambda d: checklist.repro_parts(d) >= 1),
        "requirements": _pct(items, checklist.has_requirements_list),
        "goal": _pct(items, checklist.has_goal),
        "outcomes": _pct(items, checklist.has_outcomes),
    }


def project_habits(tickets: list[dict]) -> dict:
    """How often each kind of ticket in this project does each thing the checklist looks for."""
    by_type: dict[str, list[dict]] = {}
    for t in tickets:
        by_type.setdefault(t.get("type") or "Unknown", []).append(t)
    habits = {kind: _habits_of(items) for kind, items in by_type.items()}
    others = [t for t in tickets if t.get("type") != "Epic"]
    if others:
        habits["_non_epic"] = _habits_of(others)
    return habits


def project_stats(tickets: list[dict]) -> dict:
    total = len(tickets)
    pct = lambda n: round(100 * n / total) if total else 0  # noqa: E731
    words: Counter = Counter()
    for t in tickets:
        words.update(
            w
            for w in re.findall(r"[a-z][a-z0-9_-]{2,}", (t.get("summary") or "").lower())
            if w not in STOPWORDS
        )
    by_component: dict[str, Counter] = {}
    for t in tickets:
        for c in t.get("components", []):
            by_component.setdefault(c, Counter())[t.get("assignee")] += 1
    descriptions = [t.get("description") or "" for t in tickets]
    return {
        "v": STATS_VERSION,
        "habits": project_habits(tickets),
        "total": total,
        "types": _top(Counter(t.get("type") for t in tickets)),
        "statuses": _top(Counter(t.get("status") for t in tickets)),
        "priorities": _top(Counter(t.get("priority") for t in tickets)),
        "labels": _top(Counter(label for t in tickets for label in t.get("labels", [])), 15),
        "components": _top(Counter(c for t in tickets for c in t.get("components", [])), 15),
        "assignees": _top(Counter(t.get("assignee") for t in tickets)),
        "terms": _top(words, 25),
        "component_owners": {c: _top(owners, 3) for c, owners in sorted(by_component.items())},
        "with_description_pct": pct(sum(1 for d in descriptions if d.strip())),
        "with_acceptance_criteria_pct": pct(sum(1 for d in descriptions if _AC.search(d))),
        "with_labels_pct": pct(sum(1 for t in tickets if t.get("labels"))),
        "with_components_pct": pct(sum(1 for t in tickets if t.get("components"))),
        "with_parent_pct": pct(sum(1 for t in tickets if t.get("parent"))),
        "avg_description_chars": round(sum(len(d) for d in descriptions) / total) if total else 0,
    }


def _fmt(pairs: list[tuple[str, int]]) -> str:
    return ", ".join(f"{k} ({v})" for k, v in pairs) or "none"


def render_summary(project: str, stats: dict) -> str:
    lines = [
        f"# {project} project profile",
        "",
        "Generated from the synced tickets; regenerate with `jira-agent kb summary`.",
        "",
        f"- Tickets: {stats['total']}",
        f"- Issue types: {_fmt(stats['types'])}",
        f"- Statuses: {_fmt(stats['statuses'])}",
        f"- Priorities: {_fmt(stats['priorities'])}",
        f"- Labels: {_fmt(stats['labels'])}",
        f"- Components: {_fmt(stats['components'])}",
        f"- Most active assignees: {_fmt(stats['assignees'])}",
        "",
        "## Conventions observed",
        "",
        f"- Have a description: {stats['with_description_pct']}%",
        f"- Have acceptance criteria: {stats['with_acceptance_criteria_pct']}%",
        f"- Have labels: {stats['with_labels_pct']}%",
        f"- Have components: {stats['with_components_pct']}%",
        f"- Have a parent (epic/story): {stats['with_parent_pct']}%",
        f"- Average description length: {stats['avg_description_chars']} characters",
        "",
        "## Frequent terms in summaries",
        "",
        _fmt(stats["terms"]),
        "",
        "## Component owners (by assignment count)",
        "",
    ]
    owners = stats["component_owners"]
    lines += [f"- {c}: {_fmt(o)}" for c, o in owners.items()] or ["- none"]
    return "\n".join(lines) + "\n"


def get_stats(kb: KbStore, project: str) -> dict:
    """Project statistics, cached in the index so a query does not rescan every ticket."""
    raw = kb.get_meta(f"stats:{project}")
    if raw:
        try:
            cached = json.loads(raw)
            if cached.get("v") == STATS_VERSION:
                return cached
        except json.JSONDecodeError:
            pass
    stats = json.loads(json.dumps(project_stats(kb.all_tickets(project))))
    kb.set_meta(f"stats:{project}", json.dumps(stats))
    return stats


def write_project_summary(kb: KbStore, project: str) -> Path:
    stats = project_stats(kb.all_tickets(project))
    kb.set_meta(f"stats:{project}", json.dumps(stats))
    path = kb.root / "projects" / f"{project}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_summary(project, stats), encoding="utf-8")
    return path
