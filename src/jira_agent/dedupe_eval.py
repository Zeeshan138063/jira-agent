"""Measure how well duplicate detection works, using labelled ticket pairs.

Labels never live in the repository: they describe real tickets, so they are read from a file the
user keeps next to their knowledge base (`<kb_path>/eval/labels.json`). Ground truth comes from
several sources that are reported separately, because they differ in how much to trust them:

    link       Jira "Duplicate" links (few, but certain)
    clone-link Jira "Cloners" links, counted as only "related": many teams clone a ticket to start
               from a template and then rewrite it, so a clone is not evidence of a duplicate
    labels     pairs a person (or an assistant, flagged in the file) labelled by hand
    synthetic  real tickets reworded programmatically at three difficulty levels (duplicates by
               construction; tests robustness to wording, not real-world frequency)
    random     random pairs of tickets, assumed different
    boilerplate  identical titles under different parents, assumed different (template tasks)
"""

import json
import random
import re
import statistics
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from jira_agent.dedupe import CANDIDATES, candidate_query, similarity
from jira_agent.kb.store import KbStore

LABEL_DUPLICATE, LABEL_RELATED, LABEL_DIFFERENT = "duplicate", "related", "different"
LABELS = (LABEL_DUPLICATE, LABEL_RELATED, LABEL_DIFFERENT)
DUPLICATE_LINKS = {"duplicates", "is duplicated by"}
CLONE_LINKS = {"clones", "is cloned by"}
THRESHOLDS = [round(0.1 * i, 2) for i in range(1, 10)] + [0.55, 0.65, 0.75, 0.85]
MIN_POSITIVES = 30

SYNONYMS = {
    "create": ["add", "new"],
    "add": ["create", "new"],
    "delete": ["remove"],
    "remove": ["delete"],
    "show": ["display"],
    "display": ["show"],
    "edit": ["update", "change"],
    "update": ["edit", "change"],
    "fix": ["resolve", "repair"],
    "error": ["failure", "problem"],
    "list": ["table", "overview"],
    "page": ["screen", "view"],
    "user": ["person", "account"],
    "send": ["deliver", "email"],
    "allow": ["enable", "let"],
    "cancel": ["abort", "stop"],
    "payment": ["billing", "charge"],
    "login": ["sign in"],
    "button": ["action", "control"],
}

Scorer = Callable[[dict, dict], float]


@dataclass
class Pair:
    a: str
    b: str
    label: str
    source: str
    note: str = ""


def draft_of(ticket: dict) -> dict:
    return {"summary": ticket.get("summary") or "", "description": ticket.get("description") or ""}


def _key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def pairs_from_links(tickets: list[dict]) -> list[Pair]:
    known = {t["key"] for t in tickets}
    seen, out = set(), []
    for t in tickets:
        for link in t.get("links", []):
            if link["key"] not in known or link["type"] not in DUPLICATE_LINKS | CLONE_LINKS:
                continue
            k = _key(t["key"], link["key"])
            if k not in seen:
                seen.add(k)
                if link["type"] in DUPLICATE_LINKS:
                    out.append(Pair(k[0], k[1], LABEL_DUPLICATE, "link", link["type"]))
                else:
                    out.append(Pair(k[0], k[1], LABEL_RELATED, "clone-link", link["type"]))
    return out


def load_labels(path: Path) -> list[Pair]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    pairs = []
    for row in data.get("pairs", []):
        if row.get("label") in LABELS:
            pairs.append(
                Pair(
                    row["a"],
                    row["b"],
                    row["label"],
                    row.get("source", "labels"),
                    row.get("note", ""),
                )
            )
    return pairs


def save_labels(path: Path, pairs: list[Pair], note: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"a": p.a, "b": p.b, "label": p.label, "source": p.source, "note": p.note}
        for p in sorted(pairs, key=lambda p: (p.a, p.b))
    ]
    path.write_text(json.dumps({"note": note, "pairs": rows}, indent=1) + "\n", encoding="utf-8")


def _norm_title(summary: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (summary or "").lower()).strip()


def _is_boilerplate_pair(a: dict, b: dict) -> bool:
    title = _norm_title(a.get("summary"))
    return (
        len(title) > 12
        and title == _norm_title(b.get("summary"))
        and bool(a.get("parent"))
        and bool(b.get("parent"))
        and a["parent"] != b["parent"]
    )


def boilerplate_negatives(tickets: list[dict], limit: int, rng: random.Random) -> list[Pair]:
    """Identical titles under different parents: template tasks, assumed not to be duplicates."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in tickets:
        title = _norm_title(t.get("summary"))
        if len(title) > 12:
            groups[title].append(t)
    found = []
    for group in groups.values():
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                if a.get("parent") and b.get("parent") and a["parent"] != b["parent"]:
                    found.append(Pair(*_key(a["key"], b["key"]), LABEL_DIFFERENT, "boilerplate"))
    rng.shuffle(found)
    return found[:limit]


def random_negatives(
    tickets: list[dict], n: int, rng: random.Random, exclude: set[tuple[str, str]]
) -> list[Pair]:
    out, seen, tries = [], set(exclude), 0
    while len(out) < n and tries < n * 20 and len(tickets) > 1:
        tries += 1
        a, b = rng.sample(tickets, 2)
        k = _key(a["key"], b["key"])
        if k not in seen:
            seen.add(k)
            out.append(Pair(k[0], k[1], LABEL_DIFFERENT, "random"))
    return out


def _words(text: str) -> list[str]:
    return text.split()


def perturb(ticket: dict, level: int, rng: random.Random) -> dict:
    """Reword a real ticket into a plausible duplicate. Level 1 light, 2 moderate, 3 heavy."""
    keep, swap = {1: (0.9, 0.2), 2: (0.7, 0.5), 3: (0.5, 0.8)}[level]
    summary = []
    for word in _words(ticket.get("summary") or ""):
        if rng.random() > keep:
            continue
        options = SYNONYMS.get(word.lower())
        summary.append(rng.choice(options) if options and rng.random() < swap else word)
    if not summary:
        summary = _words(ticket.get("summary") or "")[:1]
    desc = _words(ticket.get("description") or "")
    share = {1: 1.0, 2: 0.5, 3: 0.2}[level]
    desc = [w for w in desc[: max(int(len(desc) * share), 1)] if rng.random() <= keep]
    return {"summary": " ".join(summary), "description": " ".join(desc)}


def synthetic_drafts(
    tickets: list[dict], n_per_level: int, rng: random.Random
) -> list[tuple[dict, str, str]]:
    """(draft, original key, source) triples."""
    eligible = [t for t in tickets if len((t.get("summary") or "").split()) >= 3]
    out = []
    for level in (1, 2, 3):
        for t in rng.sample(eligible, min(n_per_level, len(eligible))):
            out.append((perturb(t, level, rng), t["key"], f"synthetic-L{level}"))
    return out


def candidate_pairs(
    kb: KbStore,
    tickets: list[dict],
    project: str,
    min_score: float,
    limit: int,
    skip: set[tuple[str, str]],
    scorer: Scorer = similarity,
) -> list[dict]:
    """The most similar pairs not yet labelled, for a person to review."""
    by_key = {t["key"]: t for t in tickets}
    seen, found = set(skip), []
    for t in tickets:
        draft = draft_of(t)
        for hit in kb.search(candidate_query(draft), CANDIDATES, project, mode="any"):
            other = by_key.get(hit["key"])
            if other is None or other["key"] == t["key"]:
                continue
            k = _key(t["key"], other["key"])
            if k in seen or _is_boilerplate_pair(t, other):
                continue
            seen.add(k)
            score = scorer(draft, other)
            if score >= min_score:
                found.append(
                    {
                        "a": k[0],
                        "b": k[1],
                        "score": score,
                        "summary_a": by_key[k[0]].get("summary"),
                        "summary_b": by_key[k[1]].get("summary"),
                        "label": "",
                    }
                )
    found.sort(key=lambda r: r["score"], reverse=True)
    return found[:limit]


def _stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    at = lambda q: ordered[min(len(ordered) - 1, int(q * len(ordered)))]  # noqa: E731
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 3),
        "p10": round(at(0.10), 3),
        "p50": round(at(0.50), 3),
        "p90": round(at(0.90), 3),
    }


def sweep(pos: list[float], neg: list[float], thresholds=THRESHOLDS) -> list[dict]:
    rows = []
    for th in thresholds:
        tp = sum(1 for s in pos if s >= th)
        fp = sum(1 for s in neg if s >= th)
        fn = len(pos) - tp
        precision = tp / (tp + fp) if tp + fp else None
        recall = tp / len(pos) if pos else None
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision and recall and (precision + recall)
            else 0.0
        )
        rows.append(
            {
                "threshold": th,
                "precision": None if precision is None else round(precision, 3),
                "recall": None if recall is None else round(recall, 3),
                "f1": round(f1, 3),
                "tp": tp,
                "fp": fp,
                "fn": fn,
            }
        )
    return rows


def rank_of(
    kb: KbStore, draft: dict, target: str, project: str, exclude: str | None, scorer: Scorer
) -> int | None:
    """Rank of the true duplicate among scored candidates (1 = best); None if never retrieved."""
    scored = []
    for hit in kb.search(candidate_query(draft), CANDIDATES, project, mode="any"):
        if hit["key"] == exclude:
            continue
        ticket = kb.get(hit["key"])
        if ticket:
            scored.append((scorer(draft, ticket), hit["key"]))
    scored.sort(key=lambda x: x[0], reverse=True)
    for rank, (_, key) in enumerate(scored, 1):
        if key == target:
            return rank
    return None


def _retrieval(ranks: list[int | None], ks=(1, 3, 5)) -> dict:
    n = len(ranks)
    if not n:
        return {"n": 0}
    return {
        "n": n,
        "in_candidate_pool_pct": round(100 * sum(1 for r in ranks if r) / n),
        "recall_at": {k: round(100 * sum(1 for r in ranks if r and r <= k) / n) for k in ks},
        "mrr": round(sum(1 / r for r in ranks if r) / n, 3),
    }


def evaluate(
    kb: KbStore,
    project: str,
    pairs: list[Pair],
    synthetic: list[tuple[dict, str, str]],
    scorer: Scorer = similarity,
    default_threshold: float = 0.6,
) -> dict:
    tickets = {t["key"]: t for t in kb.all_tickets(project)}
    scores: dict[str, list[float]] = defaultdict(list)  # by label
    by_source: dict[str, list[float]] = defaultdict(list)
    ranks_by_source: dict[str, list[int | None]] = defaultdict(list)
    counts: dict = defaultdict(lambda: defaultdict(int))

    for p in pairs:
        a, b = tickets.get(p.a), tickets.get(p.b)
        if a is None or b is None:
            continue
        s = scorer(draft_of(a), b)
        counts[p.label][p.source] += 1
        scores[p.label].append(s)
        by_source[f"{p.source}:{p.label}"].append(s)
        if p.label == LABEL_DUPLICATE:
            ranks_by_source[p.source].append(rank_of(kb, draft_of(a), p.b, project, p.a, scorer))

    for draft, key, source in synthetic:
        original = tickets.get(key)
        if original is None:
            continue
        s = scorer(draft, original)
        counts[LABEL_DUPLICATE][source] += 1
        scores[LABEL_DUPLICATE].append(s)
        by_source[f"{source}:{LABEL_DUPLICATE}"].append(s)
        ranks_by_source[source].append(rank_of(kb, draft, key, project, None, scorer))

    pos, neg = scores[LABEL_DUPLICATE], scores[LABEL_DIFFERENT]
    table = sweep(pos, neg)
    best = max(table, key=lambda r: r["f1"], default=None)
    at_default = (
        next((r for r in table if r["threshold"] == default_threshold), None)
        or sweep(pos, neg, [default_threshold])[0]
    )
    all_ranks = [r for rs in ranks_by_source.values() for r in rs]
    warnings = []
    real_pos = sum(
        n for src, n in counts[LABEL_DUPLICATE].items() if not src.startswith("synthetic")
    )
    if real_pos < MIN_POSITIVES:
        warnings.append(
            f"only {real_pos} real (non-synthetic) duplicate pairs: treat thresholds and recall as "
            f"indicative, not precise (aim for {MIN_POSITIVES}+)"
        )
    if not neg:
        warnings.append("no 'different' pairs: precision cannot be measured")
    return {
        "project": project,
        "counts": {label: dict(srcs) for label, srcs in counts.items()},
        "warnings": warnings,
        "score_by_label": {label: _stats(v) for label, v in scores.items()},
        "score_by_source": {k: _stats(v) for k, v in sorted(by_source.items())},
        "sweep": table,
        "best_f1": best,
        "at_default_threshold": at_default,
        "retrieval": _retrieval(all_ranks),
        "retrieval_by_source": {k: _retrieval(v) for k, v in sorted(ranks_by_source.items())},
    }


def render_table(report: dict) -> str:
    lines = [f"Duplicate detection evaluation: {report['project']}", ""]
    for label in LABELS:
        srcs = report["counts"].get(label)
        if srcs:
            lines.append(f"{label:10s} " + ", ".join(f"{k} {v}" for k, v in sorted(srcs.items())))
    for w in report["warnings"]:
        lines.append(f"WARNING: {w}")
    lines += ["", "threshold  precision  recall    f1   (duplicate vs different)"]
    for r in report["sweep"]:
        p = "  -  " if r["precision"] is None else f"{r['precision']:.2f}"
        rc = "  -  " if r["recall"] is None else f"{r['recall']:.2f}"
        lines.append(f"  {r['threshold']:.2f}       {p:>5}     {rc:>5}   {r['f1']:.2f}")
    best, cur = report["best_f1"], report["at_default_threshold"]
    if best:
        lines.append(f"\nbest F1 {best['f1']:.2f} at threshold {best['threshold']:.2f}")
    lines.append(
        f"at the default {cur['threshold']:.2f}: "
        f"precision {cur['precision']} recall {cur['recall']}"
    )
    ret = report["retrieval"]
    if ret.get("n"):
        lines.append(
            f"\nfinding the true duplicate among candidates: "
            f"{ret['in_candidate_pool_pct']}% retrieved, "
            f"recall@1 {ret['recall_at'][1]}% @3 {ret['recall_at'][3]}% @5 {ret['recall_at'][5]}%, "
            f"MRR {ret['mrr']}"
        )
        for src, r in report["retrieval_by_source"].items():
            lines.append(
                f"  {src:14s} n={r['n']:3d}  @1 {r['recall_at'][1]:3d}%  @5 {r['recall_at'][5]:3d}%"
            )
    return "\n".join(lines)
