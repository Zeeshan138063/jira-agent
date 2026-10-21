import json
import time

import pytest

from jira_agent.auth.keychain import MemoryStore
from jira_agent.cli import run
from jira_agent.config import Profile, ProfileStore
from jira_agent.dedupe import find_duplicates
from jira_agent.kb import summarize
from jira_agent.kb.cards import card, card_line, clip, is_resolved, snippet
from jira_agent.kb.context import (
    LEVELS,
    build_context,
    estimate_tokens,
    relevant_epics,
    render,
)
from jira_agent.kb.epics import epic_cards, epic_digest
from jira_agent.kb.store import KbStore

SITE = "https://acme.atlassian.net"
GOOD = (
    "As a clinic admin I want X so that Y.\n\n## Acceptance criteria\n"
    "- Given a, when b, then c.\n\nOut of scope: nothing. Depends on PROJ-1."
)


def tk(jira_id, key, summary, type_="Story", parent=None, status="To Do", cat="new", **kw):
    return {
        "id": str(jira_id),
        "key": key,
        "project": key.rsplit("-", 1)[0],
        "summary": summary,
        "description": kw.pop("description", "Some description of the work."),
        "type": type_,
        "status": status,
        "status_category": cat,
        "priority": "Medium",
        "parent": parent,
        "labels": [],
        "components": [],
        "comments": [],
        "history": [],
        "links": [],
        "updated": kw.pop("updated", "2026-10-01T10:00:00.000+0500"),
        **kw,
    }


@pytest.fixture
def kb(tmp_path):
    with KbStore(tmp_path / "kb") as store:
        yield store


def seed(kb):
    kb.upsert(
        tk(
            1,
            "PROJ-1",
            "Appointments payments and refunds",
            "Epic",
            description="### USER STORY\n\nPAYMENTS\n\nAs a user I want to manage payments.",
        )
    )
    kb.upsert(tk(2, "PROJ-2", "Rota and shifts scheduling", "Epic", description="Plan shifts."))
    kb.upsert(
        tk(
            10,
            "PROJ-10",
            "Auto refund cancelled appointments",
            parent="PROJ-1",
            description=GOOD,
            status="Done",
            cat="done",
        )
    )
    kb.upsert(tk(11, "PROJ-11", "Partial refund webhook amount", parent="PROJ-1", description=GOOD))
    kb.upsert(tk(12, "PROJ-12", "Refund window cash or Stripe", "Task", parent="PROJ-1"))
    kb.upsert(tk(20, "PROJ-20", "Shift swap between clinicians", parent="PROJ-2", description=GOOD))
    kb.upsert(tk(21, "PROJ-21", "Rota publish notifications", "Task", parent="PROJ-2"))
    kb.commit()
    kb.set_meta("synced_at:PROJ", str(int(time.time())))


# --- cards ------------------------------------------------------------------------------------


def test_snippet_drops_headings_labels_and_bullets():
    text = "### USER STORY\n\nREQUIREMENTS\n\n- first point\n- second point\nPlain sentence."
    assert snippet(text, 200) == "first point second point Plain sentence."
    assert snippet(text, 10) == "first poi…"
    assert snippet(text, 0) == "" and snippet(None, 50) == ""


def test_clip_collapses_whitespace_and_truncates():
    assert clip("a   b\n c", 50) == "a b c" and clip("abcdefghij", 5) == "abcd…"


def test_is_resolved_uses_resolution_or_status_category():
    assert is_resolved({"resolution": "Fixed"}) and is_resolved({"status_category": "done"})
    assert not is_resolved({"status_category": "indeterminate"}) and not is_resolved({})


def test_card_is_compact_and_renders_one_line():
    t = tk(
        1,
        "PROJ-1",
        "x" * 200,
        parent="PROJ-9",
        labels=["a", "b", "c", "d", "e"],
        assignee="Pat",
        description="### H\n\nBody text here.",
    )
    c = card(t, 50, 20)
    assert len(c["summary"]) == 50 and c["epic"] == "PROJ-9" and c["labels"] == ["a", "b", "c", "d"]
    assert c["gist"] == "Body text here." and c["resolved"] is False
    assert card_line(c).startswith("PROJ-1 Story/To Do [PROJ-9]:")
    assert "gist" not in card(t, 50, 0)


# --- store helpers ----------------------------------------------------------------------------


def test_search_any_mode_finds_partial_matches_that_auto_mode_misses(kb):
    kb.upsert(tk(1, "PROJ-1", "Login fails on iOS"))
    assert kb.search("login nonexistentword", mode="auto")[0]["key"] == "PROJ-1"
    kb.upsert(tk(2, "PROJ-2", "Login screen", description="nothing else"))
    both = {h["key"] for h in kb.search("login ios", mode="any")}
    assert both == {"PROJ-1", "PROJ-2"}
    assert [h["key"] for h in kb.search("login ios", mode="auto")] == ["PROJ-1"]


def test_children_epics_type_filter_and_recent_of_type(kb):
    seed(kb)
    assert {c["key"] for c in kb.children("PROJ-1")} == {"PROJ-10", "PROJ-11", "PROJ-12"}
    assert {e["key"] for e in kb.epics("PROJ")} == {"PROJ-1", "PROJ-2"}
    assert [h["key"] for h in kb.search("shifts", type_="Epic")] == ["PROJ-2"]
    assert {t["type"] for t in kb.recent_of_type("Task", "PROJ")} == {"Task"}


def test_project_stats_are_cached_in_the_index(kb, monkeypatch):
    seed(kb)
    first = summarize.get_stats(kb, "PROJ")
    monkeypatch.setattr(summarize, "project_stats", lambda t: pytest.fail("rescanned"))
    assert summarize.get_stats(kb, "PROJ") == first and kb.get_meta("stats:PROJ")


# --- epic digests -----------------------------------------------------------------------------


def test_epic_digest_summarises_children_without_the_full_text(kb):
    seed(kb)
    d = epic_digest(kb, "PROJ-1", max_children=2)
    assert (d["children"], d["done"], d["done_pct"]) == (3, 1, 33)
    assert d["goal"] == "As a user I want to manage payments."
    assert [c["key"] for c in d["child_cards"]][-1] != "PROJ-10"  # open work listed first
    assert d["children_hidden"] == 1 and d["by_status"] == {"To Do": 2, "Done": 1}
    assert epic_digest(kb, "PROJ-404") is None


def test_epic_digest_with_no_children(kb):
    kb.upsert(tk(2, "PROJ-2", "Empty epic", "Epic"))
    d = epic_digest(kb, "PROJ-2")
    assert d["children"] == 0 and d["done_pct"] is None and d["child_cards"] == []


def test_epic_cards_open_only_and_newest_first(kb):
    kb.upsert(tk(1, "PROJ-1", "Old epic", "Epic", updated="2026-01-01T00:00:00.000+0500"))
    kb.upsert(tk(2, "PROJ-2", "New epic", "Epic", updated="2026-10-01T00:00:00.000+0500"))
    kb.upsert(tk(3, "PROJ-3", "Finished epic", "Epic", status="Done", cat="done"))
    assert {e["key"] for e in epic_cards(kb, "PROJ")} == {"PROJ-1", "PROJ-2", "PROJ-3"}
    open_only = epic_cards(kb, "PROJ", open_only=True)
    assert [e["key"] for e in open_only] == ["PROJ-2", "PROJ-1"]


# --- context pack -----------------------------------------------------------------------------


def draft(summary, **kw):
    return {"summary": summary, "description": kw.pop("description", ""), **kw}


def test_context_gives_verdict_related_parent_and_conventions(kb):
    seed(kb)
    pack = build_context(kb, draft("Refund cancelled appointments automatically"), ["PROJ"], 0.6)
    assert pack["related"][0]["key"] == "PROJ-10"
    assert pack["suggested_parent"]["key"] == "PROJ-1"
    assert "sit under it" in pack["suggested_parent"]["because"]
    assert [e["key"] for e in pack["epics"]][0] == "PROJ-1"
    assert pack["kb"]["PROJ"]["tickets"] == 7 and pack["warnings"] == []
    assert "with_parent_pct" in pack["conventions"]


def test_context_flags_a_near_duplicate_and_whether_it_is_resolved(kb):
    seed(kb)
    pack = build_context(kb, draft("Auto refund cancelled appointments"), ["PROJ"], 0.6)
    assert pack["verdict"] == "likely_duplicate" and pack["closest"] == "PROJ-10"
    assert pack["closest_resolved"] is True


def test_context_for_an_unrelated_draft_has_no_verdict_or_parent(kb):
    seed(kb)
    pack = build_context(kb, draft("Quantum flux capacitor calibration"), ["PROJ"], 0.6)
    assert pack["verdict"] == "none" and pack["suggested_parent"] is None and pack["related"] == []


def test_exemplars_are_well_groomed_same_type_and_not_the_duplicate(kb):
    seed(kb)
    pack = build_context(
        kb, draft("Refund cancelled appointments", type="Story"), ["PROJ"], 0.9, fmt="json"
    )
    keys = [x["key"] for x in pack["examples"]]
    assert keys and all(x["groom_score"] >= 70 for x in pack["examples"])
    assert "PROJ-12" not in keys  # a Task, and a poorly groomed one
    strict = build_context(
        kb, draft("Auto refund cancelled appointments", type="Story"), ["PROJ"], 0.5
    )
    assert strict["examples"] and "PROJ-10" not in [x["key"] for x in strict["examples"]]


def test_exemplars_fall_back_to_the_best_groomed_recent_tickets(kb):
    seed(kb)
    pack = build_context(kb, draft("Quantum flux capacitor", type="Story"), ["PROJ"], 0.6)
    assert {x["key"] for x in pack["examples"]} <= {"PROJ-10", "PROJ-11", "PROJ-20"}
    assert pack["examples"]


def test_context_excludes_the_ticket_it_was_built_from(kb):
    seed(kb)
    pack = build_context(
        kb, draft("Auto refund cancelled appointments"), ["PROJ"], 0.6, exclude="PROJ-10"
    )
    assert "PROJ-10" not in [r["key"] for r in pack["related"]]


def test_context_warns_when_the_knowledge_base_was_never_synced(kb):
    kb.upsert(tk(1, "PROJ-1", "Some ticket"))
    pack = build_context(kb, draft("Some ticket"), ["PROJ"], 0.6)
    assert any("never synced" in w for w in pack["warnings"])


def big_kb(kb, epics=20, per=30):
    n = 0
    for e in range(epics):
        n += 1
        kb.upsert(tk(n, f"PROJ-{n}", f"Epic about topic{e} area", "Epic"))
        parent = f"PROJ-{n}"
        for s in range(per):
            n += 1
            kb.upsert(
                tk(
                    n,
                    f"PROJ-{n}",
                    f"Story {s} for topic{e} refund flow",
                    parent=parent,
                    description=GOOD if s % 3 == 0 else "short",
                )
            )
    kb.commit()
    kb.set_meta("synced_at:PROJ", str(int(time.time())))
    return n


def test_budget_is_respected_by_trimming_in_order(kb):
    big_kb(kb)
    d = draft("Refund flow for topic3", type="Story")
    sizes = []
    for budget in (6000, 1500, 800, 500):
        pack = build_context(kb, d, ["PROJ"], 0.6, budget=budget)
        meta = pack["budget"]
        assert meta["estimated_tokens"] <= budget and not meta["over_budget"]
        assert estimate_tokens(render(pack, "json")) == meta["estimated_tokens"]
        sizes.append(meta["estimated_tokens"])
    assert sizes == sorted(sizes, reverse=True) and sizes[0] > sizes[-1]
    small = build_context(kb, d, ["PROJ"], 0.6, budget=500)["budget"]
    assert small["trimmed"]


def test_an_impossible_budget_returns_the_leanest_pack_and_says_so(kb):
    big_kb(kb)
    pack = build_context(kb, draft("Refund flow for topic3"), ["PROJ"], 0.6, budget=20)
    assert pack["budget"]["over_budget"] is True and len(pack["related"]) <= 3
    assert pack["budget"]["estimated_tokens"] > 20


def test_levels_shrink_monotonically():
    assert [lv.related for lv in LEVELS] == sorted((lv.related for lv in LEVELS), reverse=True)
    assert LEVELS[0].excerpt > LEVELS[-1].excerpt == 0


def test_markdown_format_is_compact_and_has_the_key_sections(kb):
    seed(kb)
    d = draft("Auto refund cancelled appointments", type="Story")
    pack = build_context(kb, d, ["PROJ"], 0.6, fmt="markdown")
    text = render(pack, "markdown")
    for heading in (
        "# Context for:",
        "verdict: likely_duplicate",
        "## Related tickets",
        "## Suggested parent: PROJ-1",
        "## Project conventions",
    ):
        assert heading in text
    assert "tokens" in text.splitlines()[-1]
    assert pack["budget"]["estimated_tokens"] == estimate_tokens(text)


def test_context_scales_to_thousands_of_tickets(kb):
    big_kb(kb, epics=60, per=50)  # about 3,000 tickets
    start = time.monotonic()
    pack = build_context(kb, draft("Refund flow for topic7", type="Story"), ["PROJ"], 0.6)
    assert time.monotonic() - start < 3.0
    assert pack["related"] and pack["epics"]


def test_relevant_epics_rank_by_topic_not_recency(kb):
    seed(kb)
    kb.upsert(
        tk(3, "PROJ-3", "Unrelated newest epic", "Epic", updated="2027-01-01T00:00:00.000+0500")
    )
    found = relevant_epics(kb, draft("Refund cancelled appointments"), ["PROJ"], 0.6)
    assert [e["key"] for e in found][:1] == ["PROJ-1"]


def test_dedupe_now_finds_candidates_that_share_only_some_words(kb):
    kb.upsert(tk(1, "PROJ-1", "Archive staff member from a location", description="archive"))
    result = find_duplicates(
        kb, {"summary": "Archived staff: hide them from the location list"}, ["PROJ"], 0.3
    )
    assert result["matches"] and result["matches"][0]["key"] == "PROJ-1"


# --- CLI --------------------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path):
    kb_dir = tmp_path / "kb"
    with KbStore(kb_dir) as store:
        store.bind_site(SITE)
        seed(store)
    profiles = ProfileStore(tmp_path / "home")
    profiles.save(Profile(name="p", site_url=SITE, projects=["PROJ"], kb_path=str(kb_dir)))
    profiles.set_last_used("p")
    return profiles, MemoryStore()


def test_cli_context_json_and_markdown(env, capsys):
    profiles, secrets = env
    assert (
        run(
            [
                "kb",
                "context",
                "--summary",
                "Auto refund cancelled appointments",
                "--budget",
                "1500",
            ],
            profiles,
            secrets,
        )
        == 0
    )
    pack = json.loads(capsys.readouterr().out)
    assert pack["verdict"] == "likely_duplicate" and pack["budget"]["limit"] == 1500
    assert (
        run(
            ["kb", "context", "--summary", "Refund cancelled appointments", "--format", "markdown"],
            profiles,
            secrets,
        )
        == 0
    )
    assert capsys.readouterr().out.startswith("# Context for:")


def test_cli_context_from_an_existing_ticket_excludes_it(env, capsys):
    profiles, secrets = env
    assert run(["kb", "context", "--key", "proj-10"], profiles, secrets) == 0
    pack = json.loads(capsys.readouterr().out)
    assert "PROJ-10" not in [r["key"] for r in pack["related"]]
    assert run(["kb", "context", "--key", "PROJ-404"], profiles, secrets) == 2
    assert run(["kb", "context"], profiles, secrets) == 2


def test_cli_card_epic_and_epics(env, capsys):
    profiles, secrets = env
    assert run(["kb", "card", "PROJ-10", "proj-99"], profiles, secrets) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["cards"][0]["key"] == "PROJ-10" and out["missing"] == ["PROJ-99"]
    assert run(["kb", "epic", "PROJ-1", "--children", "1"], profiles, secrets) == 0
    assert len(json.loads(capsys.readouterr().out)["child_cards"]) == 1
    assert run(["kb", "epic", "PROJ-404"], profiles, secrets) == 2
    assert run(["kb", "epics", "--open"], profiles, secrets) == 0
    assert {e["key"] for e in json.loads(capsys.readouterr().out)} == {"PROJ-1", "PROJ-2"}


def test_groom_questions_rank_parent_epics_by_relevance(env, capsys):
    profiles, secrets = env
    with KbStore(profiles.get("p").kb_path) as kb:
        kb.upsert(
            tk(3, "PROJ-3", "Unrelated newest epic", "Epic", updated="2027-01-01T00:00:00.000+0500")
        )
        kb.commit()
    assert run(["groom", "questions", "--round", "1"], profiles, secrets) == 0
    recent = json.loads(capsys.readouterr().out)["questions"][2]["options"][0]["value"]
    assert recent == "PROJ-3"
    assert (
        run(
            ["groom", "questions", "--round", "1", "--summary", "Refund cancelled appointments"],
            profiles,
            secrets,
        )
        == 0
    )
    ranked = json.loads(capsys.readouterr().out)["questions"][2]["options"][0]["value"]
    assert ranked == "PROJ-1"
