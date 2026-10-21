import re

from jira_agent.kb.cards import is_resolved
from jira_agent.kb.store import KbStore, terms
from jira_agent.kb.summarize import STOPWORDS
from jira_agent.term_weights import (  # noqa: F401
    TermWeights,
    body_terms,
    project_of,
    stem,
    term_set,
    weights_for,
)

RELATED_FACTOR = 0.6
CANDIDATES = 40
LIVE_TERMS = 8
DESCRIPTION_CHARS = 600


def _dice(a: set[str], b: set[str], w=None) -> float:
    weight = w.weight if w else (lambda _t: 1.0)
    total = sum(map(weight, a)) + sum(map(weight, b))
    return 2 * sum(map(weight, a & b)) / total if a and b and total else 0.0


def _jaccard(a: set[str], b: set[str], w=None) -> float:
    weight = w.weight if w else (lambda _t: 1.0)
    union = sum(map(weight, a | b))
    return sum(map(weight, a & b)) / union if a and b and union else 0.0


def similarity(draft: dict, ticket: dict, weights: TermWeights | None = None) -> float:
    """0-1 similarity of a draft and a ticket. With `weights` (learned from the project), shared
    template words count little, differing distinctive words count a lot, and a title that repeats
    across the project is not taken as proof of a duplicate."""
    s_draft, s_ticket = term_set(draft.get("summary")), term_set(ticket.get("summary"))
    template = weights is not None and weights.is_template(draft.get("summary"))
    if s_draft and s_draft == s_ticket and not template:
        return 1.0
    body = _jaccard(body_terms(draft), body_terms(ticket), weights)
    if template:
        return round(body * 0.5, 3)
    return round(0.65 * _dice(s_draft, s_ticket, weights) + 0.35 * body, 3)


def _reasons(draft: dict, ticket: dict) -> list[str]:
    reasons = []
    shared = sorted(term_set(draft.get("summary")) & term_set(ticket.get("summary")))
    if shared:
        reasons.append("shared summary terms: " + ", ".join(shared[:6]))
    for field in ("components", "labels"):
        common = sorted(set(draft.get(field) or []) & set(ticket.get(field) or []))
        if common:
            reasons.append(f"same {field}: {', '.join(common)}")
    return reasons


def _is_closed(ticket: dict) -> bool:
    return is_resolved(ticket)


def candidate_query(draft: dict) -> str:
    """Search text for finding candidates: the draft's own words, common words removed."""
    text = f"{draft.get('summary') or ''} {(draft.get('description') or '')[:200]}"
    return " ".join(t for t in terms(text) if t not in STOPWORDS)


def live_jql(draft: dict, projects: list[str]) -> str | None:
    words = sorted(term_set(draft.get("summary")), key=len, reverse=True)[:LIVE_TERMS]
    if not words:
        return None
    scope = ", ".join(f'"{p}"' for p in projects)
    return f'project in ({scope}) AND text ~ "{" ".join(words)}" ORDER BY updated DESC'


def rank(
    draft: dict,
    tickets: list[dict],
    threshold: float,
    limit: int,
    exclude: str | None = None,
    weights: dict[str, TermWeights] | None = None,
) -> list[dict]:
    scored = []
    for t in tickets:
        if t["key"] == exclude:
            continue
        score = similarity(draft, t, (weights or {}).get(project_of(t["key"])))
        if score < threshold * RELATED_FACTOR:
            continue
        verdict = "likely_duplicate" if score >= threshold else "related"
        scored.append(
            {
                "key": t["key"],
                "summary": t.get("summary"),
                "status": t.get("status"),
                "type": t.get("type"),
                "resolved": _is_closed(t),
                "similarity": score,
                "verdict": verdict,
                "reasons": _reasons(draft, t),
                "source": t.get("_source", "kb"),
            }
        )
    scored.sort(key=lambda c: c["similarity"], reverse=True)
    return scored[:limit]


def find_duplicates(
    kb: KbStore,
    draft: dict,
    projects: list[str],
    threshold: float,
    limit: int = 5,
    live_tickets: list[dict] | None = None,
    exclude: str | None = None,
) -> dict:
    pool: dict[str, dict] = {}
    query = candidate_query(draft)
    for project in projects:
        for hit in kb.search(query, CANDIDATES, project, mode="any"):
            ticket = kb.get(hit["key"])
            if ticket:
                pool[ticket["key"]] = {**ticket, "_source": "kb"}
    for ticket in live_tickets or []:
        pool.setdefault(ticket["key"], {**ticket, "_source": "live"})
    matches = rank(draft, list(pool.values()), threshold, limit, exclude, weights_for(kb, projects))
    verdict = "none"
    if matches:
        verdict = "likely_duplicate" if matches[0]["verdict"] == "likely_duplicate" else "related"
    return {"verdict": verdict, "threshold": threshold, "matches": matches}


def draft_from_args(summary: str | None, description: str | None) -> dict:
    if not summary or not re.search(r"\w", summary):
        raise ValueError("a summary is required to check for duplicates")
    return {"summary": summary, "description": description or ""}
