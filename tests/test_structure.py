import itertools
import json
import time
from pathlib import Path

import httpx
import pytest
import respx

from jira_agent.auth import oauth
from jira_agent.auth.keychain import MemoryStore, token_key
from jira_agent.cli import run
from jira_agent.client.gate import WriteGate
from jira_agent.client.http import JiraClient
from jira_agent.client.issues import IssueAPI
from jira_agent.config import Profile, ProfileStore, WriteMode
from jira_agent.kb.store import KbStore
from jira_agent.structure import structure_example_path
from jira_agent.structure.apply import (
    apply_plan,
    load_state,
    node_extra,
    state_path,
    tree_preview,
    validate_remote,
)
from jira_agent.structure.patterns import project_patterns
from jira_agent.structure.plan import Link, Plan, SubTask, check_structure, flatten, load_plan

SITE = "https://acme.atlassian.net"
BASE = f"{oauth.API_BASE}/cloud-1/rest/api/3"


def make_plan(**over):
    data = json.loads(structure_example_path().read_text())
    data.update(over)
    return Plan.model_validate(data)


def test_example_plan_is_valid():
    plan = make_plan()
    assert check_structure(plan) == {"errors": [], "warnings": []}
    assert [n.kind for n in flatten(plan)] == [
        "epic",
        "story",
        "subtask",
        "subtask",
        "story",
        "story",
    ]


def test_structural_errors():
    plan = make_plan()
    plan.stories[1].id = "s1"
    plan.stories[0].description = ""
    plan.stories[2].links = []
    plan.stories[1].links[0].to = "ghost"
    errors = check_structure(plan)["errors"]
    assert "duplicate id: s1" in errors
    assert any("s1: story needs a description" in e for e in errors)
    assert any("'ghost' is not in the plan" in e for e in errors)


def test_blocking_cycle_detected_both_directions():
    plan = make_plan()
    plan.stories[2].links = [Link(type="Blocks", to="s2")]
    assert any("blocking cycle" in e for e in check_structure(plan)["errors"])
    plan.stories[2].links = [Link(type="Is blocked by", to="s2")]
    assert not any("cycle" in e for e in check_structure(plan)["errors"])


def test_epic_and_epic_key_conflict_and_no_stories():
    plan = make_plan(epic_key="PROJ-5")
    assert "give either epic or epic_key, not both" in check_structure(plan)["errors"]
    empty = make_plan(stories=[])
    assert "the plan has no stories" in check_structure(empty)["errors"]


def test_warnings_for_oversized_and_orphaned_work():
    plan = make_plan(epic=None)
    plan.stories[0].subtasks = [SubTask(id=f"t{i}", summary="x") for i in range(9)]
    warnings = check_structure(plan)["warnings"]
    assert any("9 sub-tasks" in w for w in warnings) and any(
        "without a parent epic" in w for w in warnings
    )


def test_node_extra_inherits_labels_from_parent_and_adds_parent_key():
    plan = make_plan()
    nodes = {n.id: n for n in flatten(plan)}
    story_extra = node_extra(nodes["s1"], nodes, "PROJ-1")
    assert story_extra == {"labels": "mobile", "parent": "PROJ-1"}
    plan.stories[0].labels = ["api"]
    nodes = {n.id: n for n in flatten(plan)}
    assert node_extra(nodes["s1"], nodes, "PROJ-1")["labels"] == "api"
    assert node_extra(nodes["s1-ui"], nodes, "PROJ-2")["labels"] == "api"


def test_sub_tasks_inherit_from_the_epic_through_their_story():
    plan = make_plan()
    plan.epic.priority = "High"
    nodes = {n.id: n for n in flatten(plan)}
    sub = node_extra(nodes["s1-ui"], nodes, "PROJ-2")
    assert sub == {"labels": "mobile", "priority": "High", "parent": "PROJ-2"}
    plan.stories[0].labels = ["api"]
    nodes = {n.id: n for n in flatten(plan)}
    assert node_extra(nodes["s1-ui"], nodes, "PROJ-2")["labels"] == "api"
    plan.stories[0].subtasks[1].labels = ["own"]
    nodes = {n.id: n for n in flatten(plan)}
    assert node_extra(nodes["s1-ui"], nodes, "PROJ-2")["labels"] == "own"


def test_tree_preview_marks_created_and_existing_epic():
    plan = make_plan(epic=None, epic_key="PROJ-5")
    lines = tree_preview(plan, {"s1": "PROJ-11"})
    assert lines[0] == "(existing epic PROJ-5)"
    assert "  Story: Show a reschedule button on scheduled deliveries [PROJ-11]" in lines
    assert "    (Blocks -> s3)" in lines


def test_patterns_from_ticket_history():
    tickets = [
        {"key": "A-1", "type": "Epic", "summary": "E", "labels": ["x"]},
        {"key": "A-2", "type": "Story", "summary": "Add export", "parent": "A-1", "labels": ["x"]},
        {"key": "A-3", "type": "Story", "summary": "Add import", "parent": "A-1", "labels": []},
        {"key": "A-4", "type": "Sub-task", "summary": "Write tests for export", "parent": "A-2"},
        {"key": "A-5", "type": "Sub-task", "summary": "Write tests for import", "parent": "A-3"},
        {"key": "A-6", "type": "Story", "summary": "Lonely"},
    ]
    p = project_patterns(tickets)
    assert p["epics"] == 1 and p["uses_subtasks"]
    assert p["stories_per_epic"]["median"] == 2
    assert p["subtasks_per_story"]["median"] == 1
    assert p["typical_subtask_titles"] == [{"pattern": "write tests for", "count": 2}]
    assert p["children_inherit_epic_labels_pct"] == 50
    assert p["orphan_stories_pct"] == 33


def test_patterns_empty_project():
    p = project_patterns([])
    assert p["epics"] == 0 and p["stories_per_epic"] is None and not p["uses_subtasks"]


FIELDS = [
    {"fieldId": "summary", "name": "Summary", "required": True, "schema": {"type": "string"}},
    {"fieldId": "description", "name": "Description", "schema": {"type": "doc"}},
    {"fieldId": "labels", "name": "Labels", "schema": {"type": "array", "items": "string"}},
    {"fieldId": "priority", "name": "Priority", "schema": {"type": "priority"}},
]
PARENT = {
    "fieldId": "parent",
    "name": "Parent",
    "schema": {"type": "issuelink", "system": "parent"},
}


def mock_jira(mock, no_parent_on=()):
    types = [
        {"id": "1", "name": "Epic"},
        {"id": "2", "name": "Story"},
        {"id": "3", "name": "Sub-task", "subtask": True},
    ]
    mock.get(f"{BASE}/issue/createmeta/PROJ/issuetypes").respond(
        json={"issueTypes": types, "total": 3}
    )
    for t in types:
        fields = FIELDS if t["name"] in no_parent_on else [*FIELDS, PARENT]
        mock.get(f"{BASE}/issue/createmeta/PROJ/issuetypes/{t['id']}").respond(
            json={"fields": fields, "total": len(fields)}
        )
    mock.get(f"{BASE}/issueLinkType").respond(
        json={
            "issueLinkTypes": [{"name": "Blocks", "inward": "is blocked by", "outward": "blocks"}]
        }
    )


@pytest.fixture
def api():
    s = MemoryStore()
    oauth.save_client(s, "cid", "csecret", 0)
    s.set(
        token_key(SITE),
        {
            "access_token": "t",
            "refresh_token": "r",
            "expires_at": time.time() + 3600,
            "cloud_id": "cloud-1",
            "site_url": SITE,
        },
    )
    gate = WriteGate(WriteMode.CONFIRM_EACH, confirmed=True, site_url=SITE)
    gate.audit_path = Path(__import__("tempfile").mkdtemp()) / "audit.log"
    return IssueAPI(JiraClient(SITE, s, sleep=lambda x: None), gate)


@respx.mock
def test_validate_remote_passes_for_good_plan(api):
    mock_jira(respx)
    assert validate_remote(api, make_plan(), {}) == []


@respx.mock
def test_validate_remote_flags_missing_parent_field_and_types_and_links(api):
    mock_jira(respx, no_parent_on=("Story",))
    plan = make_plan()
    plan.stories[1].type = "Initiative"
    plan.stories[2].links = [Link(type="Duplicates", to="s1")]
    errors = validate_remote(api, plan, {})
    assert any(e.startswith("s1:") and "'parent' is not available" in e for e in errors)
    assert any("s2: issue type 'Initiative'" in e for e in errors)
    assert any("unknown link type 'duplicates'" in e for e in errors)


@respx.mock
def test_subtask_type_falls_back_to_the_projects_subtask_type(api):
    mock_jira(respx)
    plan = make_plan(subtask_type="Subtask")
    assert validate_remote(api, plan, {}) == []


def issue_poster(fail_on: int | None = None):
    counter = itertools.count(10)
    bodies = []

    def handler(request):
        n = next(counter)
        if fail_on is not None and n == fail_on:
            return httpx.Response(400, json={"errorMessages": ["boom"]})
        bodies.append(json.loads(request.content)["fields"])
        return httpx.Response(201, json={"key": f"PROJ-{n}"})

    return handler, bodies


@respx.mock
def test_apply_creates_in_order_with_parents_and_links(api, tmp_path):
    mock_jira(respx)
    handler, bodies = issue_poster()
    respx.post(f"{BASE}/issue").mock(side_effect=handler)
    link = respx.post(f"{BASE}/issueLink").respond(201)
    state_file = tmp_path / "plan.json.state.json"
    result = apply_plan(api, make_plan(), state_file, {})
    assert result["complete"] and result["created"]["e1"] == "PROJ-10"
    assert [b["summary"] for b in bodies][:3] == [
        "Let customers reschedule deliveries in the app",
        "Show a reschedule button on scheduled deliveries",
        "Expose reschedule eligibility in the API",
    ]
    assert bodies[0].get("parent") is None
    assert bodies[1]["parent"] == {"key": "PROJ-10"}
    assert bodies[2]["parent"] == {"key": "PROJ-11"}
    sent = json.loads(link.calls[0].request.content)
    # the plan says s2 Blocks s3, so s2 is the `inwardIssue` (Jira's wording for the blocker)
    assert sent["inwardIssue"]["key"] == result["created"]["s2"]
    assert sent["outwardIssue"]["key"] == result["created"]["s3"]
    assert load_state(state_file)["created"] == result["created"]


@respx.mock
def test_apply_resumes_without_duplicating_after_failure(api, tmp_path):
    mock_jira(respx)
    state_file = tmp_path / "p.state.json"
    handler, bodies = issue_poster(fail_on=12)
    route = respx.post(f"{BASE}/issue").mock(side_effect=handler)
    respx.post(f"{BASE}/issueLink").respond(201)
    first = apply_plan(api, make_plan(), state_file, {})
    assert not first["complete"] and "boom" in first["failure"]
    assert set(first["created"]) == {"e1", "s1"}

    route.mock(side_effect=issue_poster()[0])
    second = apply_plan(api, make_plan(), state_file, {})
    assert second["complete"] and len(second["created"]) == 6
    assert second["created"]["e1"] == "PROJ-10" and second["created"]["s1"] == "PROJ-11"


@respx.mock
def test_apply_does_not_repeat_links_on_rerun(api, tmp_path):
    mock_jira(respx)
    respx.post(f"{BASE}/issue").mock(side_effect=issue_poster()[0])
    link = respx.post(f"{BASE}/issueLink").respond(201)
    state_file = tmp_path / "p.state.json"
    apply_plan(api, make_plan(), state_file, {})
    apply_plan(api, make_plan(), state_file, {})
    assert link.call_count == 1


@pytest.fixture
def env(tmp_path):
    kb_dir = tmp_path / "kb"
    with KbStore(kb_dir) as kb:
        kb.bind_site(SITE)
        kb.upsert(
            {
                "key": "PROJ-1",
                "project": "PROJ",
                "summary": "Unrelated invoice export",
                "type": "Task",
                "description": "",
                "comments": [],
                "labels": [],
                "components": [],
            }
        )
        kb.commit()
    store = ProfileStore(tmp_path / "home")
    store.save(Profile(name="p", site_url=SITE, projects=["PROJ"], kb_path=str(kb_dir)))
    store.set_last_used("p")
    secrets = MemoryStore()
    oauth.save_client(secrets, "cid", "csecret", 0)
    secrets.set(
        token_key(SITE),
        {
            "access_token": "t",
            "refresh_token": "r",
            "expires_at": time.time() + 3600,
            "cloud_id": "cloud-1",
            "site_url": SITE,
        },
    )
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(structure_example_path().read_text())
    return store, secrets, plan_file


def test_cli_validate_offline(env, capsys):
    store, secrets, plan_file = env
    assert run(["structure", "validate", str(plan_file), "--offline"], store, secrets) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["counts"] == {"epic": 1, "story": 3, "subtask": 2}
    assert any("offline" in w for w in out["warnings"])


def test_cli_validate_reports_structural_errors(env, capsys):
    store, secrets, plan_file = env
    data = json.loads(plan_file.read_text())
    data["stories"] = []
    plan_file.write_text(json.dumps(data))
    assert run(["structure", "validate", str(plan_file), "--offline"], store, secrets) == 2
    assert "the plan has no stories" in json.loads(capsys.readouterr().out)["errors"]


@respx.mock
def test_cli_apply_requires_confirmation_then_creates(env, capsys):
    store, secrets, plan_file = env
    mock_jira(respx)
    post = respx.post(f"{BASE}/issue").mock(side_effect=issue_poster()[0])
    respx.post(f"{BASE}/issueLink").respond(201)
    assert run(["structure", "apply", str(plan_file)], store, secrets) == 4
    preview = json.loads(capsys.readouterr().out)
    assert preview["confirmation_required"] and not post.called
    assert run(["structure", "apply", str(plan_file), "--confirm"], store, secrets) == 0
    done = json.loads(capsys.readouterr().out)
    assert done["ok"] and len(done["created"]) == 6
    assert state_path(plan_file).exists()


@respx.mock
def test_cli_apply_dry_run_never_posts(env, capsys):
    store, secrets, plan_file = env
    mock_jira(respx)
    post = respx.post(f"{BASE}/issue")
    assert (
        run(["structure", "apply", str(plan_file), "--confirm", "--dry-run"], store, secrets) == 0
    )
    assert json.loads(capsys.readouterr().out)["dry_run"] and not post.called


@respx.mock
def test_cli_apply_blocked_by_remote_errors(env, capsys):
    store, secrets, plan_file = env
    mock_jira(respx, no_parent_on=("Story",))
    post = respx.post(f"{BASE}/issue")
    assert run(["structure", "apply", str(plan_file), "--confirm"], store, secrets) == 2
    assert not post.called


def test_cli_patterns(env, capsys):
    store, secrets, _ = env
    assert run(["structure", "patterns"], store, secrets) == 0
    assert json.loads(capsys.readouterr().out)["patterns"]["epics"] == 0


def test_load_plan_errors(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        load_plan(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{oops")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_plan(bad)


def test_load_plan_reports_schema_problems_readably(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"project": "PROJ", "stories": [{"id": "bad id!", "summary": "x"}]}')
    with pytest.raises(ValueError, match=r"invalid plan: stories\.0\.id"):
        load_plan(bad)
