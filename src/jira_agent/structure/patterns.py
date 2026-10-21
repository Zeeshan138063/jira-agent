import re
import statistics
from collections import Counter


def _is_subtask(t: dict) -> bool:
    kind = (t.get("type") or "").lower()
    return "sub" in kind and "task" in kind


def _norm_title(summary: str) -> str:
    words = re.findall(r"[a-z]+", (summary or "").lower())
    return " ".join(words[:3])


def project_patterns(tickets: list[dict]) -> dict:
    by_key = {t["key"]: t for t in tickets}
    children: dict[str, list[dict]] = {}
    for t in tickets:
        if t.get("parent"):
            children.setdefault(t["parent"], []).append(t)

    epics = [t for t in tickets if t.get("type") == "Epic"]
    epic_sizes = [len(children.get(e["key"], [])) for e in epics if children.get(e["key"])]
    stories = [t for t in tickets if t.get("type") not in ("Epic",) and not _is_subtask(t)]
    subtasks = [t for t in tickets if _is_subtask(t)]
    sub_counts = [
        len([c for c in children.get(s["key"], []) if _is_subtask(c)])
        for s in stories
        if any(_is_subtask(c) for c in children.get(s["key"], []))
    ]
    inherited, compared = 0, 0
    for epic in epics:
        for child in children.get(epic["key"], []):
            if epic.get("labels"):
                compared += 1
                inherited += bool(set(epic["labels"]) & set(child.get("labels", [])))

    def spread(values: list[int]) -> dict | None:
        if not values:
            return None
        ordered = sorted(values)
        return {
            "median": statistics.median(ordered),
            "p90": ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))],
            "max": ordered[-1],
            "samples": len(ordered),
        }

    return {
        "epics": len(epics),
        "stories_per_epic": spread(epic_sizes),
        "uses_subtasks": bool(subtasks),
        "subtasks_per_story": spread(sub_counts),
        "typical_subtask_titles": [
            {"pattern": p, "count": c}
            for p, c in Counter(_norm_title(t["summary"]) for t in subtasks).most_common(8)
            if p and c > 1
        ],
        "typical_story_titles": [
            {"pattern": p, "count": c}
            for p, c in Counter(_norm_title(t["summary"]) for t in stories).most_common(8)
            if p and c > 2
        ],
        "children_inherit_epic_labels_pct": round(100 * inherited / compared) if compared else None,
        "orphan_stories_pct": round(
            100
            * sum(1 for s in stories if not s.get("parent") or s["parent"] not in by_key)
            / len(stories)
        )
        if stories
        else None,
    }
