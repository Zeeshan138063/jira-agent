import json

import pytest

from jira_agent.auth.keychain import MemoryStore
from jira_agent.cli import run
from jira_agent.config import Profile, ProfileStore
from jira_agent.groom import checklist
from jira_agent.groom.checklist import evaluate, habit_pct, habit_weight
from jira_agent.kb import summarize
from jira_agent.kb.store import KbStore
from jira_agent.kb.summarize import get_stats, project_habits, project_stats
from jira_agent.structure.apply import quality_report
from jira_agent.structure.plan import Plan

SITE = "https://acme.atlassian.net"
LONG = "Some explanation of the work that is long enough to count as a description. " * 3
EPIC_GOOD = (
    "### USER STORY\n\nAs the system, I need to stop archived staff receiving new work.\n\n"
    "### REQUIREMENTS\n\n- Hide the location\n- Block new bookings\n- Route follow-ups\n"
)
EPIC_GOAL_ONLY = "Goal: let clinics reschedule without calling. " + LONG


def by_id(result):
    return {i["id"]: i for i in result["items"]}


def habits(**by_type):
    return {"habits": by_type}


def h(n=50, **kw):
    base = {"n": n, "acceptance": 80, "scope": 80, "repro": 80, "goal": 80, "outcomes": 80}
    return {**base, **kw}


# --- weights from habits ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "pct,expected",
    [(None, 3), (100, 3), (50, 3), (49, 2), (20, 2), (19, 1), (10, 1), (9, 0), (0, 0)],
)
def test_habit_weight_tiers(pct, expected):
    assert habit_weight(3, pct) == expected


def test_habit_weight_never_rounds_a_light_check_to_zero_in_the_middle_tiers():
    assert habit_weight(1, 30) == 1 and habit_weight(1, 12) == 1 and habit_weight(1, 5) == 0


def test_habit_lookup_uses_the_type_then_the_non_epic_fallback():
    conv = habits(Story=h(40, acceptance=48), _non_epic=h(200, acceptance=10), Tiny=h(3))
    assert habit_pct(conv, "Story", "acceptance") == 48
    assert habit_pct(conv, "Tiny", "acceptance") == 10  # too few samples: use the fallback
    assert habit_pct(conv, "Spike", "acceptance") == 10
    assert habit_pct(conv, "Epic", "acceptance") is None  # epics never borrow story habits
    assert habit_pct({}, "Story", "acceptance") is None


# --- acceptance criteria adapt to the project -------------------------------------------------


def story(desc=LONG, **kw):
    return {
        "summary": "Let customers reschedule deliveries in the app",
        "type": "Story",
        "priority": "Medium",
        "description": desc,
        **kw,
    }


def test_without_habit_data_acceptance_keeps_its_full_weight():
    item = by_id(evaluate(story()))["acceptance"]
    assert (item["weight"], item["status"]) == (3, "fail")


def test_acceptance_weight_follows_how_often_the_type_does_it():
    weights = {}
    for pct in (80, 40, 15):
        conv = habits(Story=h(acceptance=pct))
        weights[pct] = by_id(evaluate(story(), conv))["acceptance"]["weight"]
    assert weights == {80: 3, 40: 2, 15: 1}


def test_a_missing_ac_is_skipped_where_the_type_never_writes_them():
    conv = habits(Task=h(acceptance=6))
    item = by_id(evaluate(story(type="Task"), conv))["acceptance"]
    assert item["status"] == "skipped" and "6%" in item["detail"]


def test_an_ac_that_is_present_still_earns_credit_where_it_is_not_a_habit():
    conv = habits(Task=h(acceptance=6))
    desc = LONG + "\n## Acceptance criteria\n- Given a, when b, then c."
    item = by_id(evaluate(story(desc, type="Task"), conv))["acceptance"]
    assert (item["status"], item["weight"]) == ("pass", 1)


def test_a_requirements_list_counts_when_the_project_does_not_write_acceptance_criteria():
    desc = LONG + "\n### REQUIREMENTS\n- one\n- two\n- three\n"
    low = habits(Story=h(acceptance=10))
    high = habits(Story=h(acceptance=80))
    assert by_id(evaluate(story(desc), low))["acceptance"]["status"] == "pass"
    assert by_id(evaluate(story(desc), high))["acceptance"]["status"] == "partial"


def test_the_same_ticket_scores_higher_when_ac_is_not_a_project_habit():
    strict = evaluate(story(), habits(Story=h(acceptance=90)))["score"]
    relaxed = evaluate(story(), habits(Story=h(acceptance=5)))["score"]
    assert relaxed > strict


def test_the_note_says_why_a_check_weighs_less():
    conv = habits(Story=h(acceptance=30))
    detail = by_id(evaluate(story(), conv))["acceptance"]["detail"]
    assert "only 30% of Story tickets here do this" in detail


# --- scope and bug reproduction ---------------------------------------------------------------


def test_scope_is_skipped_when_absent_in_projects_that_never_state_it():
    conv = habits(Story=h(scope=3))
    assert by_id(evaluate(story(), conv))["scope"]["status"] == "skipped"
    conv = habits(Story=h(scope=12))
    assert by_id(evaluate(story(), conv))["scope"]["status"] == "fail"
    present = story(LONG + "\nOut of scope: payments.")
    assert by_id(evaluate(present, habits(Story=h(scope=3))))["scope"]["status"] == "pass"


def bug(desc="It breaks sometimes."):
    return {
        "summary": "Login button does nothing on iOS",
        "type": "Bug",
        "priority": "High",
        "description": desc,
    }


def test_bug_reproduction_is_never_dropped_even_if_the_project_rarely_writes_it():
    conv = habits(Bug=h(repro=1))
    item = by_id(evaluate(bug(), conv))["repro"]
    assert item["status"] == "fail" and item["weight"] == 1
    full = by_id(evaluate(bug("Steps to reproduce: tap. Expected: login. Actual: nothing."), conv))
    assert full["repro"]["status"] == "pass"


# --- epics ------------------------------------------------------------------------------------


def epic(desc, **kw):
    return {
        "summary": "Archive staff members from a clinic location",
        "type": "Epic",
        "priority": "Medium",
        "description": desc,
        **kw,
    }


def test_epics_are_graded_on_goal_and_outcomes_not_story_checks():
    result = evaluate(epic(EPIC_GOOD), children=4)
    ids = {i["id"] for i in result["items"]}
    assert {"goal", "outcomes", "breakdown"} <= ids
    assert not ids & {"acceptance", "problem", "repro", "estimate"}
    assert result["grade"] == "ready"


def test_an_epic_with_a_goal_and_requirements_is_not_told_to_add_acceptance_criteria():
    result = evaluate(epic(EPIC_GOOD), habits(Epic=h(goal=74, outcomes=75)), children=2)
    assert "acceptance" not in {g["id"] for g in result["gaps"]}
    items = by_id(result)
    assert items["goal"]["status"] == "pass" and items["outcomes"]["status"] == "pass"


@pytest.mark.parametrize(
    "desc",
    [
        "As the system, I need to do the thing so that clinics are happy and we have enough text.",
        "As a clinic admin I want to see everything so that I can plan. " * 2,
        "The goal of this epic is to cut phone calls to the clinic by half. " * 2,
        "Objective: make rescheduling self-service for patients and staff alike in the app.",
    ],
)
def test_goal_wording_variants_are_recognised(desc):
    assert checklist.has_goal(desc)
    assert by_id(evaluate(epic(desc)))["goal"]["status"] in ("pass", "partial")


def test_an_epic_with_a_goal_but_no_outcomes_is_flagged_on_outcomes():
    result = evaluate(epic(EPIC_GOAL_ONLY))
    items = by_id(result)
    assert items["goal"]["status"] == "pass" and items["outcomes"]["status"] == "fail"
    assert (
        "metric or target" in items["outcomes"]["suggestion"]
        or "requirements" in (items["outcomes"]["suggestion"])
    )


def test_success_wording_or_ac_also_counts_as_outcomes():
    assert (
        by_id(evaluate(epic(LONG + "\nSuccess: 30% fewer calls.")))["outcomes"]["status"] == "pass"
    )
    ac = LONG + "\n## Acceptance criteria\n- Given a, when b, then c."
    assert by_id(evaluate(epic(ac)))["outcomes"]["status"] == "pass"


def test_an_empty_epic_is_poor_and_asks_for_goal_and_outcomes():
    result = evaluate(epic(""), children=0)
    assert result["grade"] == "poor"
    assert {"goal", "outcomes"} <= {g["id"] for g in result["gaps"]}


def test_outcomes_weight_follows_the_epic_habit_but_never_drops_out():
    conv = habits(Epic=h(goal=74, outcomes=3))
    item = by_id(evaluate(epic(EPIC_GOAL_ONLY), conv))["outcomes"]
    assert item["weight"] == 1 and item["status"] == "fail"


def test_breakdown_reports_children_only_when_known():
    assert by_id(evaluate(epic(EPIC_GOOD)))["breakdown"]["status"] == "skipped"
    assert by_id(evaluate(epic(EPIC_GOOD), children=0))["breakdown"]["status"] == "partial"
    assert by_id(evaluate(epic(EPIC_GOOD), children=3))["breakdown"]["detail"] == "3 child tickets"


def test_epics_still_never_need_a_parent():
    conv = {"with_parent_pct": 90}
    assert by_id(evaluate(epic(EPIC_GOOD), conv))["parent"]["status"] == "skipped"


def test_non_epic_item_order_and_ids_are_unchanged():
    ids = [i["id"] for i in evaluate(story())["items"]]
    assert ids == [
        "title",
        "problem",
        "acceptance",
        "repro",
        "scope",
        "dependencies",
        "estimate",
        "classification",
        "parent",
        "priority",
    ]


# --- measuring the habits ---------------------------------------------------------------------


def t(kind, desc, key="PROJ-1"):
    return {
        "key": key,
        "type": kind,
        "description": desc,
        "summary": "x",
        "labels": [],
        "components": [],
    }


def test_project_habits_are_measured_per_type_and_in_aggregate():
    tickets = [t("Story", LONG + "\n## Acceptance criteria\n- g", f"H-{i}") for i in range(3)]
    tickets += [t("Story", LONG, f"H-{i + 10}") for i in range(7)]
    tickets += [t("Task", "short", f"H-{i + 20}") for i in range(10)]
    tickets += [t("Epic", EPIC_GOOD, f"H-{i + 40}") for i in range(10)]
    found = project_habits(tickets)
    assert found["Story"]["n"] == 10 and found["Story"]["acceptance"] == 30
    assert found["Story"]["description"] == 100 and found["Task"]["description"] == 0
    assert found["Epic"]["goal"] == 100 and found["Epic"]["outcomes"] == 100
    assert found["_non_epic"]["n"] == 20 and "Epic" not in found["_non_epic"]
    assert project_habits([t("Epic", "x")]).get("_non_epic") is None


def test_project_stats_carry_habits_and_a_version():
    stats = project_stats([t("Story", LONG)])
    assert stats["v"] == summarize.STATS_VERSION and "Story" in stats["habits"]


def test_stale_cached_stats_without_habits_are_recomputed(tmp_path):
    with KbStore(tmp_path / "kb") as kb:
        kb.upsert({**t("Story", LONG), "id": "1", "project": "PROJ", "comments": []})
        kb.commit()
        kb.set_meta("stats:PROJ", json.dumps({"total": 1, "with_parent_pct": 5}))
        fresh = get_stats(kb, "PROJ")
        assert fresh["v"] == summarize.STATS_VERSION and "habits" in fresh
        assert json.loads(kb.get_meta("stats:PROJ"))["v"] == summarize.STATS_VERSION


# --- callers ----------------------------------------------------------------------------------


def test_structure_plans_count_their_stories_as_the_epics_breakdown():
    plan = Plan.model_validate(
        {
            "project": "PROJ",
            "epic": {
                "id": "e1",
                "summary": "Let customers reschedule deliveries in the app",
                "description": EPIC_GOOD,
            },
            "stories": [
                {
                    "id": "s1",
                    "summary": "Show a reschedule button on appointments",
                    "description": LONG,
                }
            ],
        }
    )
    report = quality_report(plan, {}, None, 0.6)
    epic_row = next(s for s in report["scores"] if s["id"] == "e1")
    assert "breakdown" not in epic_row["gaps"] and "acceptance" not in epic_row["gaps"]


@pytest.fixture
def env(tmp_path):
    kb_dir = tmp_path / "kb"
    with KbStore(kb_dir) as kb:
        kb.bind_site(SITE)
        base = {"project": "PROJ", "labels": [], "components": [], "comments": [], "history": []}
        kb.upsert(
            {
                **base,
                "id": "1",
                "key": "PROJ-1",
                "type": "Epic",
                "summary": "Archive staff members from a clinic location",
                "description": EPIC_GOOD,
                "priority": "Medium",
            }
        )
        for i in range(2, 14):
            kb.upsert(
                {
                    **base,
                    "id": str(i),
                    "key": f"PROJ-{i}",
                    "type": "Task",
                    "parent": "PROJ-1",
                    "summary": f"BE DEV task number {i}",
                    "description": "",
                    "priority": "Medium",
                }
            )
        kb.commit()
    profiles = ProfileStore(tmp_path / "home")
    profiles.save(Profile(name="p", site_url=SITE, projects=["PROJ"], kb_path=str(kb_dir)))
    profiles.set_last_used("p")
    return profiles, MemoryStore()


def test_groom_check_on_an_epic_counts_children_and_skips_story_checks(env, capsys):
    profiles, secrets = env  # no sign-in: the live fetch fails and the knowledge base copy is used
    assert run(["groom", "check", "--key", "PROJ-1"], profiles, secrets) == 0
    out = json.loads(capsys.readouterr().out)
    items = by_id(out)
    assert (
        items["breakdown"]["status"] == "pass"
        and items["breakdown"]["detail"] == "12 child tickets"
    )
    assert "acceptance" not in items and out["grade"] == "ready"
    assert "knowledge base copy" in out["warnings"][0]


def test_groom_check_adapts_to_the_projects_habits_from_the_knowledge_base(env, capsys):
    profiles, secrets = env
    assert run(["groom", "check", "--key", "PROJ-2"], profiles, secrets) == 0
    items = by_id(json.loads(capsys.readouterr().out))
    assert items["acceptance"]["status"] == "skipped"
    assert "not a habit for Task here (0%)" in items["acceptance"]["detail"]
    assert "breakdown" not in items
