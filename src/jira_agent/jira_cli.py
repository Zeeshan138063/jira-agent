import json
import sys
from pathlib import Path

from jira_agent.client import endpoints
from jira_agent.client.gate import ConfirmationRequired, WriteGate
from jira_agent.client.http import JiraClient, JiraError
from jira_agent.client.issues import IssueAPI, IssueError, normalize_issue
from jira_agent.config import Profile, ProfileError, ProfileStore, WriteMode
from jira_agent.kb.refresh import refresh_after_write


def _emit(data) -> None:
    print(json.dumps(data, indent=2))


def _kv(pairs: list[str] | None) -> dict:
    out = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise ValueError(f"--field expects key=value, got {pair!r}")
        out[key.strip()] = value
    return out


def _text(inline: str | None, file: str | None) -> str | None:
    if file:
        return Path(file).expanduser().read_text(encoding="utf-8")
    return inline


def add_parsers(sub) -> None:
    jira = sub.add_parser("jira", help="read and write Jira issues")
    jsub = jira.add_subparsers(dest="action", required=True)

    def cmd(name: str, help_text: str, write: bool = False):
        p = jsub.add_parser(name, help=help_text)
        p.add_argument("--profile", help="profile name (default: last used)")
        if write:
            p.add_argument("--confirm", action="store_true", help="execute after user approval")
            p.add_argument("--dry-run", action="store_true", help="preview only, never write")
            p.add_argument(
                "--no-kb-refresh",
                action="store_true",
                help="do not refresh the knowledge base after a successful write",
            )
        return p

    p = cmd("get", "show an issue")
    p.add_argument("key")
    p.add_argument("--no-comments", action="store_true")

    p = cmd("search", "run a JQL search")
    p.add_argument("jql")
    p.add_argument("--limit", type=int, default=50)

    p = cmd("types", "issue types creatable in a project")
    p.add_argument("project")

    p = cmd("meta", "fields and required fields for creating an issue type")
    p.add_argument("project")
    p.add_argument("issue_type")

    p = cmd("create", "create an issue", write=True)
    p.add_argument("--project")
    p.add_argument("--type", dest="issue_type")
    p.add_argument("--summary", required=True)
    p.add_argument("--description")
    p.add_argument("--description-file")
    p.add_argument("--field", action="append", help="key=value, repeatable")

    p = cmd("update", "edit an issue", write=True)
    p.add_argument("key")
    p.add_argument("--summary")
    p.add_argument("--description")
    p.add_argument("--description-file")
    p.add_argument("--field", action="append", help="key=value, repeatable")

    p = cmd("transitions", "list available transitions")
    p.add_argument("key")

    p = cmd("transition", "move an issue to another status", write=True)
    p.add_argument("key")
    p.add_argument("--to", required=True)

    p = cmd("comment", "add a comment", write=True)
    p.add_argument("key")
    p.add_argument("--body")
    p.add_argument("--body-file")

    cmd("link-types", "list issue link types")

    p = cmd("link", "link two issues", write=True)
    p.add_argument("link_type")
    p.add_argument("from_key", help="the issue that acts: `Blocks A B` means A blocks B")
    p.add_argument("to_key")

    p = cmd("catalog", "list or refresh the Jira REST endpoint catalog")
    p.add_argument("what", choices=["list", "refresh"])
    p.add_argument("--filter")


def _profile(store: ProfileStore, name: str | None) -> Profile:
    name = name or store.last_used_name()
    if not name:
        raise ProfileError("no profile selected; create one with `jira-agent profile create`")
    return store.get(name)


def run_jira(args, store: ProfileStore, secrets, http) -> int:
    profile = _profile(store, args.profile)
    if args.action == "catalog":
        if args.what == "refresh":
            path = endpoints.refresh_catalog(store.home, http)
            _emit({"ok": True, "path": str(path), "count": len(endpoints.load_catalog(store.home))})
        else:
            found = endpoints.search_catalog(endpoints.load_catalog(store.home), args.filter)
            _emit({"count": len(found), "endpoints": found})
        return 0

    mode = WriteMode.DRY_RUN if getattr(args, "dry_run", False) else profile.write_mode
    gate = WriteGate(
        mode,
        confirmed=getattr(args, "confirm", False),
        site_url=profile.site_url,
        audit_path=store.home / "audit.log",
    )
    api = IssueAPI(JiraClient(profile.site_url, secrets, http), gate)

    touched: list[str] = []
    result: dict = {}
    try:
        match args.action:
            case "get":
                _emit(api.get_issue(args.key, with_comments=not args.no_comments))
            case "search":
                rows = [normalize_issue(i) for i in api.search(args.jql, limit=args.limit)]
                _emit({"count": len(rows), "issues": rows})
            case "types":
                _emit(api.issue_types(args.project))
            case "meta":
                meta = api.create_meta(args.project, args.issue_type)
                _emit(
                    {
                        "issue_type": meta["issue_type"],
                        "required": meta["required"],
                        "fields": [
                            {
                                "key": f["fieldId"],
                                "name": f.get("name"),
                                "required": f.get("required", False),
                                "type": f.get("schema", {}).get("type"),
                            }
                            for f in meta["fields"]
                        ],
                    }
                )
            case "create":
                d = profile.issue_defaults
                defaults = {
                    "labels": ",".join(d.labels),
                    "components": ",".join(d.components),
                    "priority": d.priority,
                }
                result = api.create_issue(
                    args.project or profile.projects[0],
                    args.issue_type or d.issue_type,
                    args.summary,
                    _text(args.description, args.description_file),
                    _kv(args.field),
                    defaults,
                )
                _emit(result)
                touched = [result["key"]] if result.get("key") else []
            case "update":
                result = api.update_issue(
                    args.key,
                    args.summary,
                    _text(args.description, args.description_file),
                    _kv(args.field),
                )
                _emit(result)
                touched = [args.key]
            case "transitions":
                _emit(api.transitions(args.key))
            case "transition":
                result = api.transition(args.key, args.to)
                _emit(result)
                touched = [args.key]
            case "comment":
                body = _text(args.body, args.body_file)
                if not body:
                    raise ValueError("--body or --body-file is required")
                result = api.add_comment(args.key, body)
                _emit(result)
                touched = [args.key]
            case "link-types":
                _emit(api.link_types())
            case "link":
                result = api.link(args.link_type, args.from_key, args.to_key)
                _emit(result)
                touched = [args.from_key, args.to_key]
        if touched and not result.get("dry_run") and not args.no_kb_refresh:
            warning = refresh_after_write(profile, secrets, http, touched)
            if warning:
                print(warning, file=sys.stderr)
    except ConfirmationRequired as e:
        _emit({"confirmation_required": True, "action": e.action, "preview": e.preview})
        print("preview only; re-run with --confirm after the user approves", file=sys.stderr)
        return 4
    except JiraError as e:
        print(f"jira error: {e}", file=sys.stderr)
        return 5
    except IssueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0
