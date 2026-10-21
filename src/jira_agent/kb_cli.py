import json
import sys
import time
from pathlib import Path

from jira_agent.client.gate import WriteGate
from jira_agent.client.http import JiraClient, JiraError
from jira_agent.client.issues import IssueAPI
from jira_agent.config import Profile, ProfileError, ProfileStore, WriteMode
from jira_agent.kb.cards import card
from jira_agent.kb.context import DEFAULT_BUDGET, build_context
from jira_agent.kb.context import render as render_pack
from jira_agent.kb.epics import epic_cards, epic_digest
from jira_agent.kb.reconcile import reconcile_project
from jira_agent.kb.refresh import normalize_keys, refresh_keys
from jira_agent.kb.render import ticket_path
from jira_agent.kb.status import RECONCILE_DAYS, kb_status
from jira_agent.kb.store import KbError, KbStore
from jira_agent.kb.summarize import write_project_summary
from jira_agent.kb.sync import sync_project


def _emit(data) -> None:
    print(json.dumps(data, indent=2))


def add_parsers(sub) -> None:
    kb = sub.add_parser("kb", help="build and query the ticket knowledge base")
    ksub = kb.add_subparsers(dest="action", required=True)

    def cmd(name: str, help_text: str):
        p = ksub.add_parser(name, help=help_text)
        p.add_argument("--profile", help="profile name (default: last used)")
        return p

    p = cmd("sync", "backfill or incrementally refresh from Jira")
    p.add_argument("--project", help="one project key (default: all in the profile)")
    p.add_argument("--full", action="store_true", help="re-fetch everything")
    p.add_argument(
        "--no-prune",
        action="store_true",
        help="on a full sync, keep tickets that are no longer in the project",
    )
    p.add_argument(
        "--no-reconcile",
        action="store_true",
        help="skip the weekly deletion/move check that runs after an incremental sync",
    )

    p = cmd("refresh", "re-fetch specific tickets (one request per 50), e.g. after editing them")
    p.add_argument("keys", nargs="+")
    p.add_argument("--drop-missing", action="store_true", help="remove keys Jira no longer returns")

    p = cmd("reconcile", "find tickets deleted, moved or changed since the last sync")
    p.add_argument("--project", help="one project key (default: all in the profile)")
    p.add_argument("--dry-run", action="store_true", help="report what would change")
    p.add_argument(
        "--force", action="store_true", help="allow removing more than half the tickets at once"
    )

    cmd("status", "per-project freshness and the recommended action")

    p = cmd("search", "full-text search over synced tickets")
    p.add_argument("text")
    p.add_argument("--project")
    p.add_argument("--limit", type=int, default=10)

    p = cmd("show", "print a synced ticket as markdown")
    p.add_argument("key")

    p = cmd("context", "one bounded pack for a draft: verdict, related, parent, examples")
    p.add_argument("--summary")
    p.add_argument("--description")
    p.add_argument("--description-file")
    p.add_argument("--type", dest="issue_type", help="issue type of the draft, e.g. Story")
    p.add_argument("--key", help="use an existing ticket as the draft instead")
    p.add_argument("--project", help="one project key (default: all in the profile)")
    p.add_argument("--budget", type=int, default=DEFAULT_BUDGET, help="approximate token limit")
    p.add_argument("--format", choices=["json", "markdown"], default="json")

    p = cmd("card", "compact cards for specific tickets")
    p.add_argument("keys", nargs="+")

    p = cmd("epic", "digest of one epic: goal, progress, children")
    p.add_argument("key")
    p.add_argument("--children", type=int, default=8)

    p = cmd("epics", "list epics with progress")
    p.add_argument("--project")
    p.add_argument("--open", action="store_true", help="only unresolved epics")
    p.add_argument("--limit", type=int, default=30)

    p = cmd("summary", "regenerate and print the project profile")
    p.add_argument("--project")


def _profile(store: ProfileStore, name: str | None) -> Profile:
    name = name or store.last_used_name()
    if not name:
        raise ProfileError("no profile selected; create one with `jira-agent profile create`")
    return store.get(name)


def _project_of(key: str) -> str:
    return key.rsplit("-", 1)[0].upper()


def _context_draft(args, kb: KbStore) -> tuple[dict, str | None]:
    if args.key:
        key = normalize_keys([args.key])[0]
        ticket = kb.get(key)
        if ticket is None:
            raise KbError(f"{key} is not in the knowledge base")
        return {
            "summary": ticket.get("summary"),
            "description": ticket.get("description") or "",
            "type": ticket.get("type"),
        }, key
    if not args.summary:
        raise ValueError("provide --summary or --key")
    description = args.description
    if args.description_file:
        description = Path(args.description_file).expanduser().read_text(encoding="utf-8")
    return {
        "summary": args.summary,
        "description": description or "",
        "type": args.issue_type,
    }, None


def _api(profile: Profile, secrets, http) -> IssueAPI:
    gate = WriteGate(WriteMode.DRY_RUN, site_url=profile.site_url)
    return IssueAPI(JiraClient(profile.site_url, secrets, http), gate)


def _maintain(api, kb, project, profile, result, args, progress):
    """Weekly deletion/move check after an incremental sync (a full sync already enumerates)."""
    if args.no_reconcile or result["mode"] != "incremental":
        return None
    last = int(kb.get_meta(f"reconciled_at:{project}") or 0)
    if time.time() - last < RECONCILE_DAYS * 86400:
        return None
    try:
        done = reconcile_project(api, kb, project, profile.site_url, progress=progress)
    except KbError as e:
        return {"skipped": str(e)}
    return {k: done[k] for k in ("added", "changed", "removed", "seconds", "requests")}


def run_kb(args, store: ProfileStore, secrets, http) -> int:
    profile = _profile(store, args.profile)
    try:
        with KbStore(profile.kb_path) as kb:
            kb.bind_site(profile.site_url)
            match args.action:
                case "sync":
                    projects = [args.project.upper()] if args.project else profile.projects
                    api = _api(profile, secrets, http)
                    progress = lambda m: print(m, file=sys.stderr)  # noqa: E731
                    results = []
                    for p in projects:
                        result = sync_project(
                            api,
                            kb,
                            p,
                            profile.site_url,
                            full=args.full,
                            prune=not args.no_prune,
                            progress=progress,
                        )
                        result["maintenance"] = _maintain(
                            api, kb, p, profile, result, args, progress
                        )
                        results.append(result)
                    _emit({"ok": True, "results": results})
                case "refresh":
                    api = _api(profile, secrets, http)
                    result = refresh_keys(api, kb, args.keys, profile.site_url, args.drop_missing)
                    _emit({"ok": True, **result})
                case "reconcile":
                    projects = [args.project.upper()] if args.project else profile.projects
                    api = _api(profile, secrets, http)
                    progress = lambda m: print(m, file=sys.stderr)  # noqa: E731
                    results = [
                        reconcile_project(
                            api,
                            kb,
                            p,
                            profile.site_url,
                            force=args.force,
                            dry_run=args.dry_run,
                            progress=progress,
                        )
                        for p in projects
                    ]
                    _emit({"ok": True, "results": results})
                case "status":
                    _emit(kb_status(kb, profile))
                case "search":
                    project = args.project.upper() if args.project else None
                    hits = kb.search(args.text, args.limit, project)
                    _emit({"count": len(hits), "results": hits})
                case "show":
                    key = args.key.upper()
                    path = ticket_path(kb.root, _project_of(key), key)
                    if not path.exists():
                        print(f"error: {key} is not in the knowledge base", file=sys.stderr)
                        return 2
                    print(path.read_text(encoding="utf-8"))
                case "context":
                    projects = [args.project.upper()] if args.project else profile.projects
                    draft, exclude = _context_draft(args, kb)
                    pack = build_context(
                        kb,
                        draft,
                        projects,
                        profile.dedupe_threshold,
                        budget=args.budget,
                        fmt=args.format,
                        exclude=exclude,
                    )
                    print(render_pack(pack, args.format))
                case "card":
                    keys = normalize_keys(args.keys)
                    found = [kb.get(k) for k in keys]
                    _emit(
                        {
                            "cards": [card(t) for t in found if t],
                            "missing": [k for k, t in zip(keys, found, strict=True) if not t],
                        }
                    )
                case "epic":
                    digest = epic_digest(kb, normalize_keys([args.key])[0], args.children)
                    if digest is None:
                        print(f"error: {args.key} is not in the knowledge base", file=sys.stderr)
                        return 2
                    _emit(digest)
                case "epics":
                    project = args.project.upper() if args.project else None
                    _emit(epic_cards(kb, project, args.open, args.limit))
                case "summary":
                    for p in [args.project.upper()] if args.project else profile.projects:
                        path = write_project_summary(kb, p)
                        print(path.read_text(encoding="utf-8"))
    except (KbError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except JiraError as e:
        print(f"jira error: {e}", file=sys.stderr)
        return 5
    return 0
