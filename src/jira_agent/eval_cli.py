import json
import random
import sys
from pathlib import Path

from jira_agent.config import Profile, ProfileError, ProfileStore
from jira_agent.dedupe import similarity
from jira_agent.dedupe_eval import (
    LABEL_DIFFERENT,
    _key,
    boilerplate_negatives,
    candidate_pairs,
    evaluate,
    load_labels,
    pairs_from_links,
    random_negatives,
    render_table,
    synthetic_drafts,
)
from jira_agent.kb.store import KbError, KbStore
from jira_agent.term_weights import TermWeights


def _emit(data) -> None:
    print(json.dumps(data, indent=2))


def add_parsers(sub) -> None:
    ev = sub.add_parser("eval", help="measure how well duplicate detection works")
    esub = ev.add_subparsers(dest="action", required=True)

    def cmd(name: str, help_text: str):
        p = esub.add_parser(name, help=help_text)
        p.add_argument("--profile", help="profile name (default: last used)")
        p.add_argument("--project", help="one project key (default: the profile's first)")
        p.add_argument("--labels", help="labels file (default: <kb_path>/eval/labels.json)")
        return p

    p = cmd("dedupe", "precision, recall and the best threshold, from labelled pairs")
    p.add_argument("--no-links", action="store_true", help="ignore Jira clone/duplicate links")
    p.add_argument(
        "--synthetic", type=int, default=40, help="reworded duplicates per level (3 levels)"
    )
    p.add_argument("--negatives", type=int, default=200, help="random 'different' pairs")
    p.add_argument("--boilerplate", type=int, default=60, help="same-title, different-parent pairs")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--threshold", type=float, help="threshold to report (default: the profile's)")
    p.add_argument(
        "--scorer",
        choices=["plain", "idf", "template", "both"],
        default="both",
        help="plain = word overlap only; idf/template/both add learned word weights",
    )
    p.add_argument("--format", choices=["table", "json"], default="table")

    p = cmd("candidates", "write the most similar unlabelled pairs for a person to label")
    p.add_argument("--min-score", type=float, default=0.35)
    p.add_argument("--limit", type=int, default=80)
    p.add_argument("--out", help="where to write them (default: <kb_path>/eval/candidates.json)")


def _profile(store: ProfileStore, name: str | None) -> Profile:
    name = name or store.last_used_name()
    if not name:
        raise ProfileError("no profile selected; create one with `jira-agent profile create`")
    return store.get(name)


def run_eval(args, store: ProfileStore, secrets, http) -> int:
    profile = _profile(store, args.profile)
    project = (args.project or profile.projects[0]).upper()
    eval_dir = Path(profile.kb_path).expanduser() / "eval"
    labels_path = Path(args.labels).expanduser() if args.labels else eval_dir / "labels.json"
    try:
        with KbStore(profile.kb_path) as kb:
            kb.bind_site(profile.site_url)
            tickets = kb.all_tickets(project)
            if len(tickets) < 10:
                raise KbError(f"{project} has too few synced tickets; run `jira-agent kb sync`")
            labelled = load_labels(labels_path)
            known = {_key(p.a, p.b) for p in labelled}
            if args.action == "candidates":
                found = candidate_pairs(kb, tickets, project, args.min_score, args.limit, known)
                out = Path(args.out).expanduser() if args.out else eval_dir / "candidates.json"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(
                    json.dumps(
                        {"label_with": "duplicate | related | different", "pairs": found}, indent=1
                    )
                )
                _emit(
                    {
                        "ok": True,
                        "written": str(out),
                        "pairs": len(found),
                        "labels_file": str(labels_path),
                    }
                )
                return 0

            rng = random.Random(args.seed)
            pairs = list(labelled)
            if not args.no_links:
                pairs += [p for p in pairs_from_links(tickets) if _key(p.a, p.b) not in known]
            taken = {_key(p.a, p.b) for p in pairs}
            boiler = [
                p
                for p in boilerplate_negatives(tickets, args.boilerplate, rng)
                if _key(p.a, p.b) not in taken
            ]
            taken |= {_key(p.a, p.b) for p in boiler}
            pairs += boiler + random_negatives(tickets, args.negatives, rng, taken)
            synthetic = synthetic_drafts(tickets, args.synthetic, rng)
            weights = TermWeights.build(tickets).with_options(
                use_idf=args.scorer in ("idf", "both"),
                use_template=args.scorer in ("template", "both"),
            )
            scorer = (
                similarity
                if args.scorer == "plain"
                else lambda draft, ticket: similarity(draft, ticket, weights)
            )
            report = evaluate(
                kb,
                project,
                pairs,
                synthetic,
                scorer,
                args.threshold if args.threshold is not None else profile.dedupe_threshold,
            )
            report["labels_file"] = str(labels_path) if labelled else None
            if args.format == "json":
                _emit(report)
            else:
                print(render_table(report))
    except (KbError, ProfileError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


__all__ = ["add_parsers", "run_eval", "LABEL_DIFFERENT"]
