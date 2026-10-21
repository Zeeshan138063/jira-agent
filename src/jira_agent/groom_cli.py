import json
import sys
from pathlib import Path

from jira_agent.auth.oauth import AuthError
from jira_agent.client.gate import WriteGate
from jira_agent.client.http import JiraClient, JiraError
from jira_agent.client.issues import IssueAPI
from jira_agent.config import Profile, ProfileError, ProfileStore, WriteMode
from jira_agent.groom import checklist, questions, templates
from jira_agent.kb.context import relevant_epics
from jira_agent.kb.store import KbError, KbStore
from jira_agent.kb.summarize import project_stats


def _emit(data) -> None:
    print(json.dumps(data, indent=2))


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def add_parsers(sub) -> None:
    groom = sub.add_parser("groom", help="grooming helpers: checklist, templates, questions")
    gsub = groom.add_subparsers(dest="action", required=True)

    def cmd(name: str, help_text: str):
        p = gsub.add_parser(name, help=help_text)
        p.add_argument("--profile", help="profile name (default: last used)")
        p.add_argument("--project", help="project key (default: from --key or the profile)")
        return p

    p = cmd("check", "score a ticket or a draft against the grooming checklist")
    p.add_argument("--key", help="existing ticket (fetched live, KB as fallback)")
    p.add_argument("--summary")
    p.add_argument("--description")
    p.add_argument("--description-file")
    p.add_argument("--type", dest="issue_type", default="Task")
    p.add_argument("--priority")
    p.add_argument("--labels", help="comma-separated")
    p.add_argument("--components", help="comma-separated")
    p.add_argument("--parent")

    p = cmd("template", "print the description template for an issue type")
    p.add_argument("issue_type", nargs="?", default="Task")

    p = cmd("questions", "question round for creating a new ticket")
    p.add_argument("--round", type=int, required=True, choices=list(questions.ROUNDS))
    p.add_argument("--summary", help="draft summary: ranks parent epics by relevance, not recency")
    p.add_argument("--description")
    p.add_argument("--description-file")

    cmd("conventions", "show the project's observed conventions")


def _profile(store: ProfileStore, name: str | None) -> Profile:
    name = name or store.last_used_name()
    if not name:
        raise ProfileError("no profile selected; create one with `jira-agent profile create`")
    return store.get(name)


def _load_ticket(args, profile, kb, secrets, http, warnings: list[str]) -> dict:
    if args.key:
        key = args.key.upper()
        api = IssueAPI(JiraClient(profile.site_url, secrets, http), WriteGate(WriteMode.DRY_RUN))
        try:
            return api.get_issue(key)
        except (AuthError, JiraError) as e:
            warnings.append(f"live fetch failed ({e}); using the knowledge base copy")
            ticket = kb.get(key)
            if not ticket:
                raise ValueError(f"{key} could not be fetched and is not in the KB") from None
            return ticket
    if not args.summary:
        raise ValueError("provide --key or --summary")
    description = args.description
    if args.description_file:
        description = Path(args.description_file).expanduser().read_text()
    return {
        "summary": args.summary,
        "description": description or "",
        "type": args.issue_type,
        "priority": args.priority,
        "labels": _csv(args.labels),
        "components": _csv(args.components),
        "parent": args.parent,
        "links": [],
    }


def run_groom(args, store: ProfileStore, secrets, http) -> int:
    profile = _profile(store, args.profile)
    if args.action == "template":
        _emit(templates.load_template(args.issue_type, profile.grooming_template, store.home))
        return 0
    project = (
        args.project.upper()
        if args.project
        else args.key.rsplit("-", 1)[0].upper()
        if getattr(args, "key", None)
        else profile.projects[0]
    )
    warnings: list[str] = []
    try:
        with KbStore(profile.kb_path) as kb:
            kb.bind_site(profile.site_url)
            tickets = kb.all_tickets(project)
            stats = project_stats(tickets)
            if not tickets:
                warnings.append(f"no synced tickets for {project}; run `jira-agent kb sync` first")
            match args.action:
                case "check":
                    ticket = _load_ticket(args, profile, kb, secrets, http, warnings)
                    children = None
                    if ticket.get("type") == "Epic" and args.key:
                        children = len(kb.children(args.key.upper()))
                    result = checklist.evaluate(ticket, stats, children)
                    _emit({**result, "project": project, "warnings": warnings})
                case "questions":
                    epics = questions.recent_epics(tickets)
                    if args.summary:
                        description = args.description
                        if args.description_file:
                            description = Path(args.description_file).expanduser().read_text()
                        found = relevant_epics(
                            kb,
                            {"summary": args.summary, "description": description or ""},
                            [project],
                            profile.dedupe_threshold,
                        )
                        epics = found or epics
                    round_ = questions.build_round(
                        args.round,
                        stats,
                        epics,
                        profile.issue_defaults.issue_type,
                    )
                    _emit({**round_, "project": project, "warnings": warnings})
                case "conventions":
                    _emit({"project": project, "stats": stats, "warnings": warnings})
    except (KbError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0
