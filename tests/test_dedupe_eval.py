import random

import pytest

from jira_agent.dedupe import find_duplicates, similarity
from jira_agent.dedupe_eval import (
    LABEL_DIFFERENT,
    LABEL_DUPLICATE,
    Pair,
    boilerplate_negatives,
    evaluate,
    load_labels,
    pairs_from_links,
    random_negatives,
    save_labels,
    sweep,
    synthetic_drafts,
)
from jira_agent.kb.store import KbStore
from jira_agent.term_weights import TermWeights, get_weights, weights_for

AREAS = [
    "Team", "Billing", "Orders", "Invoice", "Catalog", "Shipping", "Returns", "Loyalty",
    "Coupons", "Reports", "Alerts", "Search", "Profile", "Audit", "Export", "Import",
    "Payroll", "Roster", "Ledger", "Inventory", "Vendors", "Tickets", "Reviews", "Gifts",
    "Wishlist", "Refunds", "Subscriptions", "Taxes", "Currency", "Warehouse",
]  # fmt: skip


def ticket(key, summary, description="", **kw):
    return {
        "key": key,
        "project": key.split("-")[0],
        "summary": summary,
        "description": description,
        "status": "To Do",
        "type": "Task",
        "labels": [],
        "components": [],
        "comments": [],
        **kw,
    }


@pytest.fixture
def templated():
    rows = []
    n = 1
    for area in AREAS:
        rows.append(ticket(f"PROJ-{n}", f"{area} Overview API", f"List {area.lower()} records"))
        n += 1
        rows.append(
            ticket(
                f"PROJ-{n}", "Backend Test Cases", "Write the test cases", **{"parent": f"E-{n}"}
            )
        )
        n += 1
    return rows


@pytest.fixture
def kb(tmp_path, templated):
    with KbStore(tmp_path / "kb") as store:
        for t in templated:
            store.upsert(t)
        store.commit()
        yield store


def test_shared_template_words_count_little(templated):
    weights = TermWeights.build(templated)
    assert weights.weight("overview") < weights.weight("billing")
    assert weights.weight("zzz-unseen") >= weights.weight("billing")


def test_repeated_titles_are_templates(templated):
    weights = TermWeights.build(templated + [ticket("PROJ-900", "Backend Test Cases")] * 2)
    assert weights.is_template("Backend Test Cases")
    assert not weights.is_template("Billing Overview API")


def test_sibling_template_scores_lower_with_weights(templated):
    weights = TermWeights.build(templated)
    draft = {"summary": "Billing Overview API", "description": "List billing records"}
    sibling = templated[0]
    same = templated[2]
    assert similarity(draft, sibling, weights) < similarity(draft, sibling)
    assert similarity(draft, same, weights) >= 0.9
    assert similarity(draft, sibling, weights) < 0.6 <= similarity(draft, same, weights)


def test_template_title_is_not_an_automatic_duplicate(templated):
    weights = TermWeights.build(templated)
    draft = {"summary": "Backend Test Cases", "description": "Check the refund flow end to end"}
    other = templated[1]
    assert similarity(draft, other) == 1.0
    assert similarity(draft, other, weights) < 0.6


def test_switches_turn_parts_off(templated):
    weights = TermWeights.build(templated)
    plain = weights.with_options(use_idf=False, use_template=False)
    draft = {"summary": "Billing Overview API", "description": ""}
    assert similarity(draft, templated[0], plain) == similarity(draft, templated[0])


def test_weights_roundtrip_and_reject_other_versions(templated):
    weights = TermWeights.build(templated)
    again = TermWeights.from_json(weights.to_json())
    assert again.n == weights.n and again.df == weights.df
    assert TermWeights.from_json('{"v": 999}') is None
    assert TermWeights.from_json("not json") is None


def test_get_weights_caches_and_rebuilds_on_drift(kb):
    first = get_weights(kb, "PROJ")
    assert kb.get_meta("weights:PROJ")
    assert get_weights(kb, "PROJ").n == first.n
    for i in range(100, 130):
        kb.upsert(ticket(f"PROJ-{i}", f"Brand new topic {i}", "something else"))
    kb.commit()
    assert get_weights(kb, "PROJ").n == first.n + 30


def test_small_projects_get_no_weights(tmp_path):
    with KbStore(tmp_path / "kb") as store:
        store.upsert(ticket("TINY-1", "Only one"))
        store.commit()
        assert weights_for(store, ["TINY"]) == {}


def test_find_duplicates_uses_weights(kb):
    draft = {"summary": "Billing Overview API", "description": "List billing records"}
    result = find_duplicates(kb, draft, ["PROJ"], threshold=0.6)
    assert result["verdict"] == "likely_duplicate"
    assert result["matches"][0]["key"] == "PROJ-3"
    others = [m for m in result["matches"][1:] if m["verdict"] == "likely_duplicate"]
    assert others == []


def test_sweep_precision_and_recall():
    rows = {r["threshold"]: r for r in sweep([0.9, 0.7, 0.4], [0.5, 0.2], [0.3, 0.6, 0.95])}
    assert rows[0.3]["recall"] == 1.0 and rows[0.3]["precision"] == 0.75
    assert rows[0.6]["precision"] == 1.0 and rows[0.6]["recall"] == pytest.approx(0.667, abs=1e-3)
    assert rows[0.95]["recall"] == 0.0 and rows[0.95]["precision"] is None


def test_labels_roundtrip_and_ignore_unknown(tmp_path):
    path = tmp_path / "eval" / "labels.json"
    save_labels(path, [Pair("PROJ-2", "PROJ-1", LABEL_DUPLICATE, "manual", "same bug")])
    assert load_labels(path)[0].note == "same bug"
    path.write_text('{"pairs": [{"a": "A-1", "b": "A-2", "label": "maybe"}]}')
    assert load_labels(path) == []
    assert load_labels(tmp_path / "missing.json") == []


def test_pairs_from_links_separates_duplicates_from_clones():
    tickets = [
        ticket("P-1", "a", links=[{"type": "duplicates", "key": "P-2"}]),
        ticket("P-2", "b", links=[{"type": "is duplicated by", "key": "P-1"}]),
        ticket("P-3", "c", links=[{"type": "clones", "key": "P-4"}]),
        ticket("P-4", "d", links=[{"type": "relates to", "key": "P-1"}]),
    ]
    pairs = pairs_from_links(tickets)
    assert [(p.label, p.source) for p in pairs] == [
        (LABEL_DUPLICATE, "link"),
        ("related", "clone-link"),
    ]


def test_boilerplate_negatives_share_a_title_not_a_parent(templated):
    pairs = boilerplate_negatives(templated, 10, random.Random(1))
    assert pairs and all(p.label == LABEL_DIFFERENT for p in pairs)


def test_evaluate_end_to_end_improves_with_weights(kb, templated):
    rng = random.Random(3)
    tickets = kb.all_tickets("PROJ")
    pairs = boilerplate_negatives(tickets, 20, rng) + random_negatives(tickets, 60, rng, set())
    synthetic = synthetic_drafts(tickets, 10, rng)
    weights = TermWeights.build(tickets)

    plain = evaluate(kb, "PROJ", pairs, synthetic, similarity, 0.6)
    weighted = evaluate(kb, "PROJ", pairs, synthetic, lambda d, t: similarity(d, t, weights), 0.6)

    assert plain["retrieval"]["n"] == weighted["retrieval"]["n"] > 0
    assert plain["warnings"]
    assert (
        weighted["at_default_threshold"]["precision"] >= plain["at_default_threshold"]["precision"]
    )
    assert weighted["retrieval"]["recall_at"][1] >= plain["retrieval"]["recall_at"][1] - 5
