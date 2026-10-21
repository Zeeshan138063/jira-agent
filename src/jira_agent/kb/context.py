import json
import math
import time
from collections import Counter
from dataclasses import dataclass

from jira_agent.dedupe import RELATED_FACTOR, candidate_query, similarity
from jira_agent.groom import checklist
from jira_agent.kb.cards import card, card_line, clip, is_resolved, snippet
from jira_agent.kb.epics import epic_digest
from jira_agent.kb.store import KbStore
from jira_agent.kb.summarize import get_stats
from jira_agent.term_weights import project_of, weights_for

POOL = 60
NOISE_FLOOR = 0.12
EPIC_CANDIDATES = 5
MIN_PARENT_SCORE = 0.3
MIN_EXEMPLAR_SCORE = 70
EXEMPLAR_POOL = 15
DEFAULT_BUDGET = 2500


@dataclass(frozen=True)
class Sizes:
    related: int = 8
    gist: int = 140
    epics: int = 3
    children: int = 5
    exemplars: int = 2
    excerpt: int = 500
    detail: bool = True


# From richest to leanest; the first one that fits the token budget wins.
LEVELS = (
    Sizes(8, 140, 3, 5, 2, 500, True),
    Sizes(8, 100, 3, 4, 2, 300, True),
    Sizes(6, 80, 2, 3, 1, 200, True),
    Sizes(5, 60, 2, 2, 1, 0, False),
    Sizes(4, 40, 1, 0, 0, 0, False),
    Sizes(3, 0, 1, 0, 0, 0, False),
    Sizes(3, 0, 0, 0, 0, 0, False),
)


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


@dataclass
class Analysis:
    draft: dict
    projects: list[str]
    threshold: float
    pool: list[dict]
    epic_scores: dict[str, float]
    epic_support: dict[str, int]
    exemplars: list[dict]
    conventions: dict
    kb_info: dict
    warnings: list[str]


def _scored_pool(kb: KbStore, draft: dict, projects: list[str], exclude: str | None) -> list[dict]:
    query = candidate_query(draft)
    seen: dict[str, dict] = {}
    weights = weights_for(kb, projects)
    for project in projects:
        for hit in kb.search(query, POOL, project, mode="any"):
            if hit["key"] == exclude or hit["key"] in seen:
                continue
            ticket = kb.get(hit["key"])
            if ticket:
                seen[hit["key"]] = {
                    "ticket": ticket,
                    "score": similarity(draft, ticket, weights.get(project_of(ticket["key"]))),
                }
    ranked = sorted(seen.values(), key=lambda r: r["score"], reverse=True)
    return [r for r in ranked if r["score"] >= NOISE_FLOOR]


def _epic_scores(kb, draft, projects, pool, top_n: int):
    scores: dict[str, float] = {}
    support: dict[str, int] = {}
    for row in pool[:top_n]:
        t = row["ticket"]
        if t.get("type") == "Epic":
            scores[t["key"]] = scores.get(t["key"], 0.0) + 0.6 * row["score"]
        elif t.get("parent"):
            scores[t["parent"]] = scores.get(t["parent"], 0.0) + row["score"]
            support[t["parent"]] = support.get(t["parent"], 0) + 1
    for project in projects:
        for hit in kb.search(candidate_query(draft), 20, project, type_="Epic", mode="any"):
            epic = kb.get(hit["key"])
            if epic:
                bonus = 0.6 * similarity(draft, epic)
                scores[epic["key"]] = max(scores.get(epic["key"], 0.0), bonus)
    return scores, support


def _pick_exemplars(
    pool: list[dict], draft_type: str | None, conventions: dict, threshold: float
) -> list[dict]:
    kinds = Counter(r["ticket"].get("type") for r in pool[:8] if r["ticket"].get("type") != "Epic")
    wanted = draft_type or (kinds.most_common(1)[0][0] if kinds else None)
    scored = []
    for row in pool[:EXEMPLAR_POOL]:
        t = row["ticket"]
        if wanted and t.get("type") != wanted or t.get("type") == "Epic":
            continue
        grade = checklist.evaluate(t, conventions)
        if grade["score"] >= MIN_EXEMPLAR_SCORE:
            scored.append({"ticket": t, "groom_score": grade["score"], "similarity": row["score"]})
    distinct = [r for r in scored if r["similarity"] < threshold]
    chosen = distinct or scored
    chosen.sort(key=lambda r: (r["groom_score"], r["similarity"]), reverse=True)
    return chosen[:2]


def _fill_exemplars(kb, have, pool, draft, projects, conventions, threshold) -> list[dict]:
    """Top up with the best-groomed recent tickets of the same type when few relate to the draft."""
    kinds = Counter(r["ticket"].get("type") for r in pool[:8] if r["ticket"].get("type") != "Epic")
    wanted = draft.get("type") or (kinds.most_common(1)[0][0] if kinds else None)
    if not wanted or wanted == "Epic":
        return have
    taken = {r["ticket"]["key"] for r in have}
    taken |= {r["ticket"]["key"] for r in pool if r["score"] >= threshold}
    extra = []
    for project in projects:
        for t in kb.recent_of_type(wanted, project, 150):
            if t["key"] in taken:
                continue
            grade = checklist.evaluate(t, conventions)
            if grade["score"] >= MIN_EXEMPLAR_SCORE:
                extra.append({"ticket": t, "groom_score": grade["score"], "similarity": 0.0})
    extra.sort(key=lambda r: r["groom_score"], reverse=True)
    return (have + extra)[:2]


def conventions_of(stats: dict) -> dict:
    keep = (
        "with_description_pct",
        "with_acceptance_criteria_pct",
        "with_labels_pct",
        "with_components_pct",
        "with_parent_pct",
        "avg_description_chars",
        "habits",
    )
    return {k: stats.get(k) for k in keep}


def analyze(
    kb: KbStore,
    draft: dict,
    projects: list[str],
    threshold: float,
    exclude: str | None = None,
) -> Analysis:
    warnings: list[str] = []
    info = {"projects": {}}
    now = time.time()
    merged_stats = {}
    for project in projects:
        raw = kb.get_meta(f"synced_at:{project}")
        info["projects"][project] = {
            "tickets": kb.count(project),
            "synced_hours_ago": round((now - int(raw)) / 3600, 1) if raw else None,
        }
        if not raw:
            warnings.append(f"{project} was never synced; run `jira-agent kb sync`")
        elif now - int(raw) > 7 * 86400:
            warnings.append(f"{project} was last synced over a week ago")
        if not merged_stats:
            merged_stats = get_stats(kb, project)
    pool = _scored_pool(kb, draft, projects, exclude)
    conventions = conventions_of(merged_stats)
    scores, support = _epic_scores(kb, draft, projects, pool, 8)
    exemplars = _pick_exemplars(pool, draft.get("type"), conventions, threshold)
    if len(exemplars) < 2:
        exemplars = _fill_exemplars(kb, exemplars, pool, draft, projects, conventions, threshold)
    return Analysis(
        draft=draft,
        projects=projects,
        threshold=threshold,
        pool=pool,
        epic_scores=scores,
        epic_support=support,
        exemplars=exemplars,
        conventions={**conventions, "stats": merged_stats},
        kb_info=info,
        warnings=warnings,
    )


def verdict_of(a: Analysis) -> dict:
    top = a.pool[0] if a.pool else None
    if top is None or top["score"] < a.threshold * RELATED_FACTOR:
        return {"verdict": "none"}
    kind = "likely_duplicate" if top["score"] >= a.threshold else "related"
    return {
        "verdict": kind,
        "closest": top["ticket"]["key"],
        "similarity": top["score"],
        "closest_resolved": is_resolved(top["ticket"]),
    }


def ranked_epics(kb: KbStore, a: Analysis, limit: int) -> list[dict]:
    rows = []
    for key, score in sorted(a.epic_scores.items(), key=lambda kv: kv[1], reverse=True):
        epic = kb.get(key)
        if epic is None or epic.get("type") != "Epic":
            continue
        rows.append({"epic": epic, "score": round(score, 3)})
        if len(rows) >= limit:
            break
    return rows


def assemble(kb: KbStore, a: Analysis, sz: Sizes) -> dict:
    related = []
    for row in a.pool[: sz.related]:
        c = card(row["ticket"], 90, sz.gist)
        c["similarity"] = row["score"]
        related.append(c)
    epics_ranked = ranked_epics(kb, a, EPIC_CANDIDATES)
    suggestion = None
    if epics_ranked and epics_ranked[0]["score"] >= MIN_PARENT_SCORE:
        best = epics_ranked[0]
        supporters = a.epic_support.get(best["epic"]["key"], 0)
        suggestion = {
            "key": best["epic"]["key"],
            "title": clip(best["epic"].get("summary"), 90),
            "score": best["score"],
            "because": f"{supporters} of the top related tickets sit under it"
            if supporters
            else "its own text matches the draft",
        }
    digests = []
    for row in epics_ranked[: sz.epics]:
        digest = epic_digest(
            kb,
            row["epic"]["key"],
            sz.children,
            goal_chars=200 if sz.detail else 120,
            epic=row["epic"],
        )
        digest["score"] = row["score"]
        digests.append(digest)
    exemplars = []
    for row in a.exemplars[: sz.exemplars]:
        t = row["ticket"]
        exemplars.append(
            {
                "key": t["key"],
                "type": t.get("type"),
                "groom_score": row["groom_score"],
                "summary": clip(t.get("summary"), 90),
                "excerpt": snippet(t.get("description"), sz.excerpt) if sz.excerpt else None,
            }
        )
    conventions = {k: v for k, v in a.conventions.items() if k not in ("stats", "habits")}
    if sz.detail:
        stats = a.conventions.get("stats") or {}
        conventions["common_types"] = [n for n, _ in stats.get("types", [])[:4]]
        conventions["common_labels"] = [n for n, _ in stats.get("labels", [])[:5]]
        conventions["common_components"] = [n for n, _ in stats.get("components", [])[:5]]
    pack = {
        "draft": {"summary": clip(a.draft.get("summary"), 120), "type": a.draft.get("type")},
        **verdict_of(a),
        "kb": a.kb_info["projects"],
        "related": related,
        "suggested_parent": suggestion,
        "epics": digests,
        "examples": exemplars,
        "conventions": conventions,
        "warnings": a.warnings,
    }
    return pack


def render_markdown(pack: dict) -> str:
    lines = [f"# Context for: {pack['draft']['summary']}"]
    verdict = pack["verdict"]
    if verdict == "none":
        lines.append("verdict: no close match in the knowledge base")
    else:
        done = " (already resolved)" if pack.get("closest_resolved") else ""
        lines.append(f"verdict: {verdict} - closest {pack['closest']} ({pack['similarity']}){done}")
    sync = ", ".join(
        f"{p}: {v['tickets']} tickets, synced {v['synced_hours_ago']}h ago"
        for p, v in pack["kb"].items()
    )
    lines.append(f"knowledge base: {sync}")
    for w in pack["warnings"]:
        lines.append(f"warning: {w}")
    if pack["related"]:
        lines += ["", "## Related tickets"]
        lines += [f"- {card_line(c)} ({c['similarity']})" for c in pack["related"]]
    sp = pack["suggested_parent"]
    if sp:
        lines += [
            "",
            f"## Suggested parent: {sp['key']} {sp['title']}",
            f"(score {sp['score']}; {sp['because']})",
        ]
    for e in pack["epics"]:
        progress = f", {e['done']}/{e['children']} done" if e["children"] else ""
        lines += ["", f"## Epic {e['key']}: {e['title']} ({e['status']}{progress})"]
        if e["goal"]:
            lines.append(f"goal: {e['goal']}")
        if e["child_cards"]:
            lines += [f"- {card_line(c)}" for c in e["child_cards"]]
        if e["children_hidden"]:
            lines.append(f"- ...{e['children_hidden']} more")
    if pack["examples"]:
        lines += ["", "## Well-groomed examples to match"]
        for x in pack["examples"]:
            lines.append(
                f"- {x['key']} {x['type']} (groom score {x['groom_score']}): {x['summary']}"
            )
            if x.get("excerpt"):
                lines.append(f"  {x['excerpt']}")
    c = pack["conventions"]
    lines += ["", "## Project conventions"]
    lines.append(
        "description {with_description_pct}% / "
        "acceptance criteria {with_acceptance_criteria_pct}% / "
        "labels {with_labels_pct}% / components {with_components_pct}% / "
        "parent {with_parent_pct}% (avg description {avg_description_chars} chars)".format(**c)
    )
    for name in ("common_types", "common_labels", "common_components"):
        if c.get(name):
            lines.append(f"{name.replace('_', ' ')}: {', '.join(c[name])}")
    meta = pack.get("budget")
    if meta:
        trimmed = f"; trimmed: {', '.join(meta['trimmed'])}" if meta["trimmed"] else ""
        lines.append(f"\n(~{meta['estimated_tokens']} of {meta['limit']} tokens{trimmed})")
    return "\n".join(lines)


def render(pack: dict, fmt: str) -> str:
    if fmt == "markdown":
        return render_markdown(pack)
    return json.dumps(pack, separators=(",", ":"))


def build_context(
    kb: KbStore,
    draft: dict,
    projects: list[str],
    threshold: float,
    budget: int = DEFAULT_BUDGET,
    fmt: str = "json",
    exclude: str | None = None,
) -> dict:
    """One bounded pack of everything a draft needs: verdict, related tickets, parent, examples."""
    a = analyze(kb, draft, projects, threshold, exclude)
    pack: dict = {}
    used = 0
    for level, sizes in enumerate(LEVELS):
        pack = assemble(kb, a, sizes)
        used = _stamp(pack, budget, _trimmed(level), fmt)
        if used <= budget:
            break
    pack["budget"]["over_budget"] = used > budget
    return pack


_TRIM_LABELS = (
    ("related", "related tickets"),
    ("epics", "epic digests"),
    ("children", "epic children"),
    ("exemplars", "examples"),
    ("excerpt", "example text"),
    ("gist", "ticket summaries"),
    ("detail", "convention detail"),
)


def _trimmed(level: int) -> list[str]:
    base, final = LEVELS[0], LEVELS[level]
    return [name for field, name in _TRIM_LABELS if getattr(final, field) != getattr(base, field)]


def _stamp(pack: dict, budget: int, trimmed: list[str], fmt: str) -> int:
    """Record the token estimate inside the pack, counting the budget block itself."""
    used = 0
    for _ in range(3):
        pack["budget"] = {
            "limit": budget,
            "estimated_tokens": used,
            "trimmed": trimmed,
            "over_budget": False,
        }
        measured = estimate_tokens(render(pack, fmt))
        if measured == used:
            break
        used = measured
    return used


def relevant_epics(
    kb: KbStore, draft: dict, projects: list[str], threshold: float, limit: int = 3
) -> list[dict]:
    """Epics most related to a draft (by its text and by where similar tickets already sit)."""
    a = analyze(kb, draft, projects, threshold)
    return [row["epic"] for row in ranked_epics(kb, a, limit) if row["score"] >= NOISE_FLOOR]
