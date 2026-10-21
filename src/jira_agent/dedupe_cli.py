import json
import sys
from pathlib import Path

from jira_agent.auth.oauth import AuthError
from jira_agent.client.gate import WriteGate
from jira_agent.client.http import JiraClient, JiraError
from jira_agent.client.issues import IssueAPI
from jira_agent.config import Profile, ProfileError, ProfileStore, WriteMode
from jira_agent.dedupe import draft_from_args, find_duplicates, live_jql
from jira_agent.kb.store import KbError, KbStore
from jira_agent.kb.sync import KB_FIELDS, kb_ticket


def _emit(data) -> None:
    print(json.dumps(data, indent=2))


def add_parsers(sub) -> None:
    p = sub.add_parser("dedupe", help="check a draft or an existing ticket for duplicates")
    p.add_argument("--profile", help="profile name (default: last used)")
    p.add_argument("--summary")
    p.add_argument("--description")
    p.add_argument("--description-file")
    p.add_argument("--key", help="check an existing ticket from the knowledge base instead")
    p.add_argument("--project", help="limit to one project (default: all in the profile)")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--threshold", type=float, help="override the profile's dedupe_threshold")
    p.add_argument("--no-live", action="store_true", help="skip the live Jira search")


def _profile(store: ProfileStore, name: str | None) -> Profile:
    name = name or store.last_used_name()
    if not name:
        raise ProfileError("no profile selected; create one with `jira-agent profile create`")
    return store.get(name)


def _live(profile: Profile, secrets, http, draft: dict, projects: list[str]):
    jql = live_jql(draft, projects)
    if not jql:
        return [], None
    api = IssueAPI(JiraClient(profile.site_url, secrets, http), WriteGate(WriteMode.DRY_RUN))
    try:
        return [kb_ticket(r) for r in api.search(jql, fields=KB_FIELDS, limit=20)], None
    except (AuthError, JiraError) as e:
        return [], f"live search skipped: {e}"


def run_dedupe(args, store: ProfileStore, secrets, http) -> int:
    profile = _profile(store, args.profile)
    projects = [args.project.upper()] if args.project else profile.projects
    threshold = profile.dedupe_threshold if args.threshold is None else args.threshold
    try:
        with KbStore(profile.kb_path) as kb:
            kb.bind_site(profile.site_url)
            exclude = None
            if args.key:
                exclude = args.key.upper()
                ticket = kb.get(exclude)
                if not ticket:
                    print(f"error: {exclude} is not in the knowledge base", file=sys.stderr)
                    return 2
                draft = draft_from_args(ticket.get("summary"), ticket.get("description"))
            else:
                description = args.description
                if args.description_file:
                    description = Path(args.description_file).expanduser().read_text()
                draft = draft_from_args(args.summary, description)
            live, warning = (
                ([], None) if args.no_live else _live(profile, secrets, http, draft, projects)
            )
            result = find_duplicates(kb, draft, projects, threshold, args.limit, live, exclude)
            warnings = [w for w in (warning,) if w]
            if kb.count() == 0:
                warnings.append(
                    "knowledge base is empty; run `jira-agent kb sync` for reliable results"
                )
            _emit({**result, "warnings": warnings})
    except (KbError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0
