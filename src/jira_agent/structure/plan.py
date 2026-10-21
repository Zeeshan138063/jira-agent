from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator

MAX_SUBTASKS = 8
MAX_STORIES = 12
BLOCKING_LINKS = {"blocks", "blocked by"}


class Item(BaseModel):
    id: str
    summary: str
    description: str = ""
    type: str | None = None
    labels: list[str] = Field(default_factory=list)
    components: list[str] = Field(default_factory=list)
    priority: str | None = None
    fields: dict[str, str] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not v or not all(c.isalnum() or c in "_-" for c in v):
            raise ValueError("id must be letters, digits, '_' or '-'")
        return v


class Link(BaseModel):
    type: str
    to: str


class SubTask(Item):
    pass


class Story(Item):
    subtasks: list[SubTask] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)


class Plan(BaseModel):
    project: str
    epic: Item | None = None
    epic_key: str | None = None
    stories: list[Story] = Field(default_factory=list)
    epic_type: str = "Epic"
    story_type: str = "Story"
    subtask_type: str = "Sub-task"

    @field_validator("project")
    @classmethod
    def _project(cls, v: str) -> str:
        return v.strip().upper()


@dataclass
class Node:
    id: str
    kind: str
    item: Item
    parent_id: str | None
    type_name: str


def load_plan(path: str | Path) -> Plan:
    try:
        text = Path(path).expanduser().read_text()
    except FileNotFoundError:
        raise ValueError(f"plan file not found: {path}") from None
    try:
        return Plan.model_validate_json(text)
    except ValidationError as e:
        problems = []
        for err in e.errors():
            where = ".".join(str(part) for part in err["loc"]) or "plan"
            if err["type"] == "json_invalid":
                raise ValueError("plan is not valid JSON") from None
            problems.append(f"{where}: {err['msg']}")
        raise ValueError("invalid plan: " + "; ".join(problems)) from None


def flatten(plan: Plan) -> list[Node]:
    nodes: list[Node] = []
    epic_id = None
    if plan.epic:
        epic_id = plan.epic.id
        nodes.append(Node(plan.epic.id, "epic", plan.epic, None, plan.epic.type or plan.epic_type))
    for story in plan.stories:
        nodes.append(Node(story.id, "story", story, epic_id, story.type or plan.story_type))
        for sub in story.subtasks:
            nodes.append(Node(sub.id, "subtask", sub, story.id, sub.type or plan.subtask_type))
    return nodes


def _blocking_cycle(plan: Plan) -> list[str] | None:
    edges: dict[str, list[str]] = {}
    for story in plan.stories:
        for link in story.links:
            if link.type.lower() == "blocks":
                edges.setdefault(story.id, []).append(link.to)
            elif link.type.lower() == "blocked by":
                edges.setdefault(link.to, []).append(story.id)
    state: dict[str, int] = {}

    def visit(node: str, path: list[str]) -> list[str] | None:
        state[node] = 1
        for nxt in edges.get(node, []):
            if state.get(nxt) == 1:
                return [*path, node, nxt]
            if nxt not in state and (cycle := visit(nxt, [*path, node])):
                return cycle
        state[node] = 2
        return None

    for start in list(edges):
        if start not in state and (cycle := visit(start, [])):
            return cycle
    return None


def check_structure(plan: Plan) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    nodes = flatten(plan)
    ids = [n.id for n in nodes]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        errors.append(f"duplicate id: {dup}")
    if plan.epic and plan.epic_key:
        errors.append("give either epic or epic_key, not both")
    if not plan.stories:
        errors.append("the plan has no stories")
    for n in nodes:
        if not n.item.summary.strip():
            errors.append(f"{n.id}: summary is empty")
        if n.kind in ("epic", "story") and not n.item.description.strip():
            errors.append(f"{n.id}: {n.kind} needs a description")
    known = set(ids)
    for story in plan.stories:
        for link in story.links:
            if link.to not in known:
                errors.append(f"{story.id}: link target {link.to!r} is not in the plan")
            elif link.to == story.id:
                errors.append(f"{story.id}: cannot link to itself")
        if len(story.subtasks) > MAX_SUBTASKS:
            warnings.append(f"{story.id}: {len(story.subtasks)} sub-tasks; consider splitting it")
    if len(plan.stories) > MAX_STORIES:
        warnings.append(f"{len(plan.stories)} stories under one epic; consider splitting the epic")
    if not plan.epic and not plan.epic_key:
        warnings.append("stories will be created without a parent epic")
    if cycle := _blocking_cycle(plan):
        errors.append("blocking cycle: " + " -> ".join(cycle))
    return {"errors": errors, "warnings": warnings}
