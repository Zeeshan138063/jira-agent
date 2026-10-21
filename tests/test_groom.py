import json
import time

import pytest
import respx

from jira_agent.auth import oauth
from jira_agent.auth.keychain import MemoryStore, token_key
from jira_agent.cli import run
from jira_agent.config import Profile, ProfileStore
from jira_agent.groom import checklist, questions, templates
from jira_agent.kb.store import KbStore

SITE = "https://acme.atlassian.net"
BASE = f"{oauth.API_BASE}/cloud-1/rest/api/3"

GOOD_STORY = {
    "key": "PROJ-1",
    "summary": "Let patients reschedule appointments from the app",
    "type": "Story",
    "priority": "Medium",
    "description": (
        "Patients call the clinic to reschedule today, which wastes staff time.\n\n"
        "## Acceptance criteria\n- Given a booked appointment, when the patient taps reschedule, "
        "then they can pick a new slot.\n\nOut of scope: group bookings. Depends on PROJ-9."
    ),
    "labels": ["mobile"],
    "components": [],
    "parent": "PROJ-100",
    "links": [{"type": "blocks", "key": "PROJ-9"}],
}
CONVENTIONS = {"with_labels_pct": 80, "with_components_pct": 10, "with_parent_pct": 70}


def by_id(result):
    return {i["id"]: i for i in result["items"]}


def test_well_formed_story_is_ready():
    result = checklist.evaluate(GOOD_STORY, CONVENTIONS)
    assert result["grade"] == "ready" and result["score"] >= 90 and result["gaps"] == []


def test_empty_ticket_is_poor_and_lists_gaps():
    result = checklist.evaluate({"summary": "fix", "type": "Task"}, CONVENTIONS)
    assert result["grade"] == "poor"
    ids = {g["id"] for g in result["gaps"]}
    assert {"title", "problem", "acceptance", "classification", "parent", "priority"} <= ids
    assert all(g["suggestion"] for g in result["gaps"])


def test_bug_needs_repro_parts_and_skips_scope():
    bug = {"summary": "Login button does nothing on iOS", "type": "Bug", "description": "It breaks"}
    items = by_id(checklist.evaluate(bug))
    assert items["repro"]["status"] == "fail" and items["scope"]["status"] == "skipped"
    full = {**bug, "description": "Steps to reproduce: tap. Expected: login. Actual: nothing."}
    assert by_id(checklist.evaluate(full))["repro"]["status"] == "pass"
    partial = {**bug, "description": "Expected: login works"}
    assert by_id(checklist.evaluate(partial))["repro"]["status"] == "partial"


def test_conventions_make_checks_adaptive():
    ticket = {**GOOD_STORY, "labels": [], "parent": None}
    assert by_id(checklist.evaluate(ticket, {}))["classification"]["status"] == "skipped"
    strict = by_id(checklist.evaluate(ticket, CONVENTIONS))
    assert strict["classification"]["status"] == "fail" and strict["parent"]["status"] == "fail"


def test_epics_never_need_a_parent():
    items = by_id(checklist.evaluate({**GOOD_STORY, "type": "Epic", "parent": None}, CONVENTIONS))
    assert items["parent"]["status"] == "skipped"


def test_estimate_only_scored_when_available():
    assert by_id(checklist.evaluate(GOOD_STORY))["estimate"]["status"] == "skipped"
    assert (
        by_id(checklist.evaluate({**GOOD_STORY, "estimate": None}))["estimate"]["status"] == "fail"
    )


def test_implied_expectations_are_partial_criteria():
    t = {**GOOD_STORY, "description": "The page should load fast and users must see totals. " * 5}
    assert by_id(checklist.evaluate(t))["acceptance"]["status"] == "partial"


def test_templates_builtin_and_custom_override(tmp_path):
    assert templates.load_template("bug")["type"] == "Bug"
    assert "Steps to reproduce" in templates.load_template("Bug")["body"]
    assert templates.load_template("unknown")["type"] == "Task"
    custom = tmp_path / "templates" / "acme"
    custom.mkdir(parents=True)
    (custom / "Story.md").write_text("custom body")
    loaded = templates.load_template("Story", "acme", tmp_path)
    assert loaded["body"] == "custom body" and loaded["source"].endswith("Story.md")
    assert templates.load_template("Story", "default", tmp_path)["source"] == "builtin"


STATS = {
    "types": [("Story", 20), ("Bug", 10), ("Epic", 3), ("Task", 5)],
    "components": [("API", 9), ("Web", 5)],
    "labels": [("mobile", 7), ("billing", 4)],
    "priorities": [("High", 3), ("Medium", 10), ("Low", 4)],
}
EPICS = [
    {"key": "PROJ-100", "type": "Epic", "summary": "Rescheduling", "updated": "2026-10-02"},
    {"key": "PROJ-101", "type": "Epic", "summary": "Old", "updated": "2026-01-01"},
    {
        "key": "PROJ-102",
        "type": "Epic",
        "summary": "Done",
        "resolution": "Done",
        "updated": "2026-10-09",
    },
]


def test_recent_epics_skip_resolved_and_sort_by_update():
    assert [e["key"] for e in questions.recent_epics(EPICS)] == ["PROJ-100", "PROJ-101"]


def test_round_one_uses_project_data():
    r = questions.build_round(1, STATS, questions.recent_epics(EPICS), "Story")
    assert [q["id"] for q in r["questions"]] == ["type", "component", "parent"]
    types = r["questions"][0]["options"]
    assert [o["value"] for o in types] == ["Story", "Bug", "Task"]
    assert types[0]["recommended"] and types[0]["label"].endswith("(Recommended)")
    assert [o["value"] for o in r["questions"][1]["options"]] == ["API", "Web", "None / not sure"]
    assert r["questions"][2]["options"][0]["value"] == "PROJ-100"


def test_questions_work_with_an_empty_knowledge_base():
    for n in questions.ROUNDS:
        r = questions.build_round(n, {}, [], "Task")
        assert len(r["questions"]) == 3
        for q in r["questions"]:
            assert 2 <= len(q["options"]) <= 4 and q["header"] and len(q["header"]) <= 12


def test_round_three_has_multiselect_labels_and_middle_priority_recommended():
    r = questions.build_round(3, STATS, [])
    priority, _, labels = r["questions"]
    assert [o["recommended"] for o in priority["options"]] == [False, True, False]
    assert labels["multi_select"] and labels["options"][-1]["value"] == "No labels"


def test_invalid_round_rejected():
    with pytest.raises(ValueError):
        questions.build_round(4, {}, [])


@pytest.fixture
def env(tmp_path):
    kb_dir = tmp_path / "kb"
    with KbStore(kb_dir) as kb:
        kb.bind_site(SITE)
        for i in range(4):
            kb.upsert(
                {
                    "key": f"PROJ-{i + 1}",
                    "project": "PROJ",
                    "summary": f"Ticket number {i}",
                    "description": "",
                    "type": "Epic" if i == 0 else "Story",
                    "labels": ["mobile"],
                    "components": ["API"],
                    "parent": None if i == 0 else "PROJ-1",
                    "updated": "2026-10-01",
                    "comments": [],
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
    return store, secrets


def test_cli_check_draft_uses_kb_conventions(env, capsys):
    store, secrets = env
    code = run(["groom", "check", "--summary", "Add thing", "--type", "Story"], store, secrets)
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["project"] == "PROJ"
    assert by_id(out)["classification"]["status"] == "fail"


@respx.mock
def test_cli_check_existing_ticket_fetches_live(env, capsys):
    store, secrets = env
    respx.get(f"{BASE}/issue/PROJ-2").respond(
        json={"key": "PROJ-2", "fields": {"summary": "Short", "issuetype": {"name": "Story"}}}
    )
    respx.get(f"{BASE}/issue/PROJ-2/comment").respond(json={"comments": []})
    run(["groom", "check", "--key", "proj-2"], store, secrets)
    out = json.loads(capsys.readouterr().out)
    assert out["key"] == "PROJ-2" and out["grade"] == "poor" and out["warnings"] == []


@respx.mock
def test_cli_check_falls_back_to_kb_when_live_fails(env, capsys):
    store, secrets = env
    respx.get(f"{BASE}/issue/PROJ-2").respond(403, json={"errorMessages": ["no"]})
    code = run(["groom", "check", "--key", "PROJ-2"], store, secrets)
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and "knowledge base copy" in out["warnings"][0]


@respx.mock
def test_cli_check_unknown_ticket_errors(env, capsys):
    store, secrets = env
    respx.get(f"{BASE}/issue/PROJ-99").respond(404, json={"errorMessages": ["gone"]})
    assert run(["groom", "check", "--key", "PROJ-99"], store, secrets) == 2


def test_cli_questions_and_template(env, capsys):
    store, secrets = env
    assert run(["groom", "questions", "--round", "1"], store, secrets) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["questions"][1]["options"][0]["value"] == "API"
    assert out["questions"][2]["options"][0]["value"] == "PROJ-1"
    assert run(["groom", "template", "Bug"], store, secrets) == 0
    assert json.loads(capsys.readouterr().out)["type"] == "Bug"


def test_cli_check_requires_key_or_summary(env):
    store, secrets = env
    assert run(["groom", "check"], store, secrets) == 2
