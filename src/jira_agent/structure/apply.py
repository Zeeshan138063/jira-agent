import json
from pathlib import Path

from jira_agent.client.issues import IssueAPI, IssueError
from jira_agent.dedupe import find_duplicates
from jira_agent.groom import checklist
from jira_agent.kb.store import KbStore
from jira_agent.structure.plan import Node, Plan, flatten

PLACEHOLDER_PARENT = "PLAN-0"
POOR_SCORE = 50


def state_path(plan_path: str | Path) -> Path:
    p = Path(plan_path).expanduser()
    return p.with_name(p.name + ".state.json")


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"created": {}, "linked": []}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2))


def _inherited(node: Node, nodes: dict[str, Node], attr: str):
    """The node's own value, else the nearest ancestor's (a sub-task inherits from the epic)."""
    while node is not None:
        if value := getattr(node.item, attr):
            return value
        node = nodes.get(node.parent_id)
    return None


def node_extra(node: Node, nodes: dict[str, Node], parent_key: str | None) -> dict:
    item = node.item

    def inherit(attr: str):
        return _inherited(node, nodes, attr)

    extra: dict = {}
    if labels := inherit("labels"):
        extra["labels"] = ",".join(labels)
    if components := inherit("components"):
        extra["components"] = ",".join(components)
    if priority := inherit("priority"):
        extra["priority"] = priority
    if parent_key:
        extra["parent"] = parent_key
    extra.update(item.fields)
    return extra


def parent_key_for(node: Node, plan: Plan, created: dict[str, str]) -> str | None:
    if node.parent_id:
        return created.get(node.parent_id, PLACEHOLDER_PARENT)
    if node.kind == "story" and plan.epic_key:
        return plan.epic_key
    return None


def quality_report(plan: Plan, conventions: dict, kb: KbStore | None, threshold: float) -> dict:
    nodes = flatten(plan)
    scores, duplicates = [], []
    for n in nodes:
        draft = {
            "summary": n.item.summary,
            "description": n.item.description,
            "type": n.type_name,
            "priority": n.item.priority or "planned",
            "labels": n.item.labels,
            "components": n.item.components,
            "parent": n.parent_id or plan.epic_key,
            "links": [],
        }
        result = checklist.evaluate(
            draft, conventions, children=len(plan.stories) if n.kind == "epic" else None
        )
        if n.kind != "subtask":
            scores.append(
                {
                    "id": n.id,
                    "score": result["score"],
                    "grade": result["grade"],
                    "gaps": [g["id"] for g in result["gaps"]],
                }
            )
        if kb is not None:
            found = find_duplicates(
                kb,
                {"summary": n.item.summary, "description": n.item.description},
                [plan.project],
                threshold,
                limit=3,
            )
            if found["verdict"] != "none":
                duplicates.append(
                    {"id": n.id, "verdict": found["verdict"], "matches": found["matches"]}
                )
    warnings = [
        f"{s['id']}: grooming score {s['score']} ({', '.join(s['gaps'])})"
        for s in scores
        if s["score"] < POOR_SCORE
    ]
    return {"scores": scores, "duplicates": duplicates, "warnings": warnings}


def resolve_type(wanted: str, kind: str, types: list[dict]) -> dict | None:
    by_name = {t["name"].lower(): t for t in types}
    if wanted.lower() in by_name:
        return by_name[wanted.lower()]
    if kind == "subtask":
        return next((t for t in types if t.get("subtask")), None)
    return None


def validate_remote(api: IssueAPI, plan: Plan, defaults: dict) -> list[str]:
    errors: list[str] = []
    nodes = flatten(plan)
    by_id = {n.id: n for n in nodes}
    types = api.issue_types(plan.project)
    metas: dict[str, dict] = {}
    for n in nodes:
        resolved = resolve_type(n.type_name, n.kind, types)
        if not resolved:
            names = ", ".join(t["name"] for t in types)
            errors.append(f"{n.id}: issue type {n.type_name!r} not in {plan.project} ({names})")
            continue
        if resolved["name"] not in metas:
            metas[resolved["name"]] = api.create_meta(plan.project, resolved["name"])
        try:
            api.prepare_create(
                plan.project,
                resolved["name"],
                n.item.summary,
                n.item.description,
                node_extra(n, by_id, parent_key_for(n, plan, {})),
                defaults,
                metas[resolved["name"]],
            )
        except IssueError as e:
            errors.append(f"{n.id}: {e}")
    wanted_links = {link.type.lower() for s in plan.stories for link in s.links}
    if wanted_links:
        known = {t["name"].lower() for t in api.link_types()}
        for missing in sorted(wanted_links - known):
            errors.append(f"unknown link type {missing!r}; available: {', '.join(sorted(known))}")
    return errors


def tree_preview(plan: Plan, created: dict[str, str] | None = None) -> list[str]:
    created = created or {}
    lines = []
    for n in flatten(plan):
        indent = {"epic": "", "story": "  ", "subtask": "    "}[n.kind]
        mark = f" [{created[n.id]}]" if n.id in created else ""
        lines.append(f"{indent}{n.type_name}: {n.item.summary}{mark}")
        if n.kind == "story":
            for link in next(s for s in plan.stories if s.id == n.id).links:
                lines.append(f"{indent}  ({link.type} -> {link.to})")
    if plan.epic_key:
        lines.insert(0, f"(existing epic {plan.epic_key})")
    return lines


def apply_plan(api: IssueAPI, plan: Plan, state_file: Path, defaults: dict) -> dict:
    nodes = flatten(plan)
    by_id = {n.id: n for n in nodes}
    state = load_state(state_file)
    created: dict[str, str] = state["created"]
    types = api.issue_types(plan.project)
    failure = None
    for n in nodes:
        if n.id in created:
            continue
        resolved = resolve_type(n.type_name, n.kind, types)
        try:
            result = api.create_issue(
                plan.project,
                resolved["name"] if resolved else n.type_name,
                n.item.summary,
                n.item.description,
                node_extra(n, by_id, parent_key_for(n, plan, created)),
                defaults,
            )
        except Exception as e:  # noqa: BLE001 - report progress, keep state for resume
            failure = f"{n.id}: {e}"
            break
        created[n.id] = result["key"]
        save_state(state_file, state)

    if not failure:
        for story in plan.stories:
            for link in story.links:
                tag = f"{story.id}|{link.type}|{link.to}"
                if tag in state["linked"]:
                    continue
                try:
                    api.link(link.type, created[story.id], created[link.to])
                except Exception as e:  # noqa: BLE001
                    failure = f"link {tag}: {e}"
                    break
                state["linked"].append(tag)
                save_state(state_file, state)
            if failure:
                break
    return {
        "complete": failure is None,
        "created": created,
        "failure": failure,
        "tree": tree_preview(plan, created),
        "state_file": str(state_file),
    }
