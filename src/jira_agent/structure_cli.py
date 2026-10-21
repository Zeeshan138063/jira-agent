import json
import sys

from jira_agent.auth.oauth import AuthError
from jira_agent.client.gate import WriteGate
from jira_agent.client.http import JiraClient, JiraError
from jira_agent.client.issues import IssueAPI
from jira_agent.config import Profile, ProfileError, ProfileStore, WriteMode
from jira_agent.kb.refresh import refresh_after_write
from jira_agent.kb.store import KbError, KbStore
from jira_agent.kb.summarize import project_stats
from jira_agent.structure import structure_example_path
from jira_agent.structure.apply import (
    apply_plan,
    load_state,
    quality_report,
    state_path,
    tree_preview,
    validate_remote,
)
from jira_agent.structure.patterns import project_patterns
from jira_agent.structure.plan import check_structure, flatten, load_plan


def _emit(data) -> None:
    print(json.dumps(data, indent=2))


def add_parsers(sub) -> None:
    st = sub.add_parser("structure", help="plan and create an epic/story/sub-task breakdown")
    ssub = st.add_subparsers(dest="action", required=True)

    def cmd(name: str, help_text: str):
        p = ssub.add_parser(name, help=help_text)
        p.add_argument("--profile", help="profile name (default: last used)")
        return p

    p = cmd("patterns", "how this project usually breaks work down (from the KB)")
    p.add_argument("--project")

    cmd("example", "print a sample plan file")

    p = cmd("validate", "check a plan file without creating anything")
    p.add_argument("plan")
    p.add_argument("--offline", action="store_true", help="skip checks against live Jira")

    p = cmd("apply", "create the plan in Jira (preview first, then --confirm)")
    p.add_argument("plan")
    p.add_argument("--confirm", action="store_true", help="create after user approval")
    p.add_argument("--dry-run", action="store_true")


def _profile(store: ProfileStore, name: str | None) -> Profile:
    name = name or store.last_used_name()
    if not name:
        raise ProfileError("no profile selected; create one with `jira-agent profile create`")
    return store.get(name)


def _defaults(profile: Profile) -> dict:
    d = profile.issue_defaults
    return {
        "labels": ",".join(d.labels),
        "components": ",".join(d.components),
        "priority": d.priority,
    }


def _report(plan, profile, kb, api, offline: bool) -> dict:
    structural = check_structure(plan)
    errors, warnings = list(structural["errors"]), list(structural["warnings"])
    if plan.project not in profile.projects:
        warnings.append(f"{plan.project} is not one of the profile's projects")
    report = {"errors": errors, "warnings": warnings, "scores": [], "duplicates": []}
    if errors:
        return report
    stats = project_stats(kb.all_tickets(plan.project))
    quality = quality_report(
        plan, stats, kb if kb.count(plan.project) else None, profile.dedupe_threshold
    )
    report.update(scores=quality["scores"], duplicates=quality["duplicates"])
    warnings += quality["warnings"]
    if not kb.count(plan.project):
        warnings.append("knowledge base is empty; duplicate and convention checks were skipped")
    if offline:
        warnings.append("offline: issue types, required fields and link types were not checked")
    else:
        errors += validate_remote(api, plan, _defaults(profile))
    return report


def run_structure(args, store: ProfileStore, secrets, http) -> int:
    if args.action == "example":
        _emit(json.loads(structure_example_path().read_text()))
        return 0
    profile = _profile(store, args.profile)
    try:
        with KbStore(profile.kb_path) as kb:
            kb.bind_site(profile.site_url)
            if args.action == "patterns":
                project = (args.project or profile.projects[0]).upper()
                _emit({"project": project, "patterns": project_patterns(kb.all_tickets(project))})
                return 0

            plan = load_plan(args.plan)
            confirmed = getattr(args, "confirm", False)
            dry = getattr(args, "dry_run", False) or profile.write_mode == WriteMode.DRY_RUN
            gate = WriteGate(
                WriteMode.CONFIRM_EACH,
                confirmed=args.action == "apply" and confirmed and not dry,
                site_url=profile.site_url,
                audit_path=store.home / "audit.log",
            )
            api = IssueAPI(JiraClient(profile.site_url, secrets, http), gate)
            offline = args.action == "validate" and args.offline
            report = _report(plan, profile, kb, api, offline)
            counts = {
                k: sum(1 for n in flatten(plan) if n.kind == k)
                for k in ("epic", "story", "subtask")
            }
            state_file = state_path(args.plan)
            created = load_state(state_file)["created"]
            base = {
                **report,
                "counts": counts,
                "tree": tree_preview(plan, created),
                "resuming": bool(created),
            }

            if args.action == "validate":
                _emit({"ok": not report["errors"], **base})
                return 0 if not report["errors"] else 2
            if report["errors"]:
                _emit({"ok": False, **base})
                return 2
            if dry:
                _emit({"ok": True, "dry_run": True, **base})
                return 0
            if not confirmed:
                _emit({"confirmation_required": True, **base})
                print(
                    "preview only; re-run with --confirm after the user approves", file=sys.stderr
                )
                return 4
            result = apply_plan(api, plan, state_file, _defaults(profile))
            _emit({"ok": result["complete"], **result, "warnings": report["warnings"]})
            if result["created"]:
                warning = refresh_after_write(
                    profile, secrets, http, list(result["created"].values())
                )
                if warning:
                    print(warning, file=sys.stderr)
            return 0 if result["complete"] else 5
    except (AuthError, JiraError) as e:
        print(f"jira error: {e}", file=sys.stderr)
        return 3 if isinstance(e, AuthError) else 5
    except (KbError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
