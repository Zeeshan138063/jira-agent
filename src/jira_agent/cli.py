import argparse
import getpass
import json
import os
import sys
import time
import webbrowser
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path

import httpx

from jira_agent import __version__, usage
from jira_agent.auth import credentials, oauth, token_login
from jira_agent.auth.callback_server import CallbackError
from jira_agent.auth.keychain import KeychainStore, SecretStoreError, token_key
from jira_agent.config import (
    IssueDefaults,
    KbMode,
    Profile,
    ProfileError,
    ProfileStore,
    SyncPolicy,
    WriteMode,
    check_local,
    export_preset,
    import_preset,
    with_overrides,
)
from jira_agent.dedupe_cli import add_parsers as add_dedupe_parsers
from jira_agent.dedupe_cli import run_dedupe
from jira_agent.eval_cli import add_parsers as add_eval_parsers
from jira_agent.eval_cli import run_eval
from jira_agent.groom_cli import add_parsers as add_groom_parsers
from jira_agent.groom_cli import run_groom
from jira_agent.jira_cli import add_parsers as add_jira_parsers
from jira_agent.jira_cli import run_jira
from jira_agent.kb_cli import add_parsers as add_kb_parsers
from jira_agent.kb_cli import run_kb
from jira_agent.session_cli import PLUGIN_ROOT, run_session
from jira_agent.session_cli import add_parsers as add_session_parsers
from jira_agent.structure_cli import add_parsers as add_structure_parsers
from jira_agent.structure_cli import run_structure

OPEN_BROWSER = webbrowser.open


def _emit(data) -> None:
    print(json.dumps(data, indent=2))


def _csv(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def _add_profile_fields(p: argparse.ArgumentParser, *, required: bool) -> None:
    p.add_argument("--site-url", required=required)
    p.add_argument("--projects", required=required, help="comma-separated project keys")
    p.add_argument("--kb-path", required=required)
    p.add_argument("--kb-mode", choices=[m.value for m in KbMode])
    p.add_argument("--sync-policy", choices=[m.value for m in SyncPolicy])
    p.add_argument("--write-mode", choices=[m.value for m in WriteMode])
    p.add_argument("--dedupe-threshold", type=float)
    p.add_argument("--grooming-template")
    p.add_argument("--issue-type")
    p.add_argument("--priority")
    p.add_argument("--labels", help="comma-separated")
    p.add_argument("--components", help="comma-separated")


def _field_values(args: argparse.Namespace) -> dict:
    values = {
        "site_url": args.site_url,
        "projects": _csv(args.projects),
        "kb_path": args.kb_path,
        "kb_mode": args.kb_mode,
        "sync_policy": args.sync_policy,
        "write_mode": args.write_mode,
        "dedupe_threshold": args.dedupe_threshold,
        "grooming_template": args.grooming_template,
    }
    defaults = {
        "issue_type": args.issue_type,
        "priority": args.priority,
        "labels": _csv(args.labels),
        "components": _csv(args.components),
    }
    defaults = {k: v for k, v in defaults.items() if v is not None}
    if defaults:
        values["issue_defaults"] = defaults
    return {k: v for k, v in values.items() if v is not None}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jira-agent")
    parser.add_argument("--version", action="version", version=f"jira-agent {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    profile = sub.add_parser("profile", help="manage saved session profiles")
    psub = profile.add_subparsers(dest="action", required=True)

    psub.add_parser("list", help="list profiles, last-used first")

    show = psub.add_parser("show")
    show.add_argument("name")

    create = psub.add_parser("create")
    create.add_argument("name")
    _add_profile_fields(create, required=True)
    create.add_argument("--overwrite", action="store_true")

    update = psub.add_parser("update")
    update.add_argument("name")
    _add_profile_fields(update, required=False)

    clone = psub.add_parser("clone")
    clone.add_argument("source")
    clone.add_argument("new_name")
    _add_profile_fields(clone, required=False)

    use = psub.add_parser("use", help="mark a profile as last used")
    use.add_argument("name")

    delete = psub.add_parser("delete")
    delete.add_argument("name")

    check = psub.add_parser("check", help="validate a profile locally")
    check.add_argument("name")

    export = psub.add_parser("export", help="write a shareable team preset (no secrets)")
    export.add_argument("name")
    export.add_argument("--output", help="write to this file instead of printing")

    imp = psub.add_parser("import", help="create a profile from a team preset file")
    imp.add_argument("file")
    imp.add_argument("--name", help="profile name (default: the preset's suggestion)")
    imp.add_argument("--kb-path", help="where to keep your ticket copy (default: ~/jira-kb/<name>)")
    imp.add_argument("--overwrite", action="store_true")

    auth = sub.add_parser("auth", help="sign in to Atlassian through the browser")
    asub = auth.add_subparsers(dest="action", required=True)

    setup = asub.add_parser("setup", help="store the OAuth app's client id and secret")
    setup.add_argument("--client-id", required=True)
    setup.add_argument("--port", type=int, default=oauth.DEFAULT_PORT)

    for name, help_text in (
        ("login", "open the browser and sign in"),
        ("status", "check the saved sign-in against Jira"),
        ("logout", "forget the saved sign-in"),
    ):
        cmd = asub.add_parser(name, help=help_text)
        cmd.add_argument("--profile", help="profile name (default: last used)")
        if name == "login":
            cmd.add_argument(
                "--method",
                choices=["token", "oauth"],
                default="token",
                help="token: paste an Atlassian API token into a local page (default); "
                "oauth: browser consent with your own OAuth app",
            )

    expiry = asub.add_parser("expiry", help="record when the saved API token expires")
    expiry.add_argument("--profile", help="profile name (default: last used)")
    when = expiry.add_mutually_exclusive_group(required=True)
    when.add_argument("--date", help="the expiry date shown on Atlassian's token page, YYYY-MM-DD")
    when.add_argument("--days-left", type=int, help="days until it expires")
    when.add_argument("--unknown", action="store_true", help="forget the recorded expiry")

    add_jira_parsers(sub)
    add_kb_parsers(sub)
    add_dedupe_parsers(sub)
    add_groom_parsers(sub)
    add_structure_parsers(sub)
    add_session_parsers(sub)
    add_eval_parsers(sub)

    usage_cmd = sub.add_parser("usage", help="show how many tokens the CLI has handed to the model")
    usage_cmd.add_argument("--days", type=int, help="only the last N days")
    usage_cmd.add_argument("--format", choices=["table", "json"], default="table")
    usage_cmd.add_argument("--static", action="store_true", help="include the instruction sizes")
    usage_cmd.add_argument("--reset", action="store_true", help="delete the usage log")
    return parser


def _set_expiry(site: str, secrets, args) -> dict:
    record = secrets.get(token_key(site))
    if credentials.method_of(record) != credentials.METHOD_TOKEN:
        raise ProfileError("no saved API token for this site; run `jira-agent auth login`")
    if args.unknown:
        record["expires_at"] = None
    elif args.date:
        day = datetime.strptime(args.date, "%Y-%m-%d").replace(hour=23, minute=59)
        record["expires_at"] = int(day.timestamp())
    else:
        record["expires_at"] = int(time.time()) + args.days_left * 86400
    record.setdefault("created_at", int(time.time()))
    secrets.set(token_key(site), record)
    return credentials.token_expiry(record)


def _read_client_secret() -> str:
    secret = os.environ.get("JIRA_AGENT_CLIENT_SECRET")
    if secret:
        return secret
    if sys.stdin.isatty():
        return getpass.getpass("Atlassian OAuth client secret: ")
    return sys.stdin.readline().strip()


def _profile_site(store: ProfileStore, name: str | None) -> str:
    name = name or store.last_used_name()
    if not name:
        raise ProfileError("no profile selected; create one with `jira-agent profile create`")
    return store.get(name).site_url


def run_auth(args, store: ProfileStore, secrets, http) -> int:
    match args.action:
        case "setup":
            secret = _read_client_secret()
            if not secret:
                print("error: client secret is empty", file=sys.stderr)
                return 2
            oauth.save_client(secrets, args.client_id, secret, args.port)
            _emit({"ok": True, "client_id": args.client_id, "port": args.port})
        case "login":
            site = _profile_site(store, args.profile)
            announce = lambda m: print(m, file=sys.stderr)  # noqa: E731
            if args.method == "oauth":
                record = oauth.login(
                    site, secrets, http=http, open_browser=OPEN_BROWSER, announce=announce
                )
            else:
                record = token_login.login_with_token(
                    site, secrets, http=http, open_browser=OPEN_BROWSER, announce=announce
                )
            _emit(
                {
                    "ok": True,
                    "method": record.get("method", "oauth"),
                    "site_url": record["site_url"],
                    "account": record.get("display_name"),
                }
            )
        case "status":
            _emit(
                {"ok": True, **oauth.preflight(_profile_site(store, args.profile), secrets, http)}
            )
        case "expiry":
            site = _profile_site(store, args.profile)
            _emit({"ok": True, "site_url": site, "expiry": _set_expiry(site, secrets, args)})
        case "logout":
            site = _profile_site(store, args.profile)
            oauth.logout(site, secrets)
            _emit({"ok": True, "signed_out_of": site})
    return 0


def _merge_defaults(profile: Profile, values: dict) -> dict:
    if "issue_defaults" in values:
        merged = {**profile.issue_defaults.model_dump(), **values["issue_defaults"]}
        values = {**values, "issue_defaults": IssueDefaults(**merged).model_dump()}
    return values


def run(argv: list[str], store: ProfileStore | None = None, secrets=None, http=None) -> int:
    args = build_parser().parse_args(argv)
    store = store or ProfileStore()
    try:
        if args.command == "auth":
            return run_auth(args, store, secrets or KeychainStore(), http)
        if args.command == "eval":
            return run_eval(args, store, secrets or KeychainStore(), http)
        if args.command == "usage":
            return run_usage(args, store.home)
        if args.command == "session":
            return run_session(args, store, secrets or KeychainStore(), http)
        if args.command == "structure":
            return run_structure(args, store, secrets or KeychainStore(), http)
        if args.command == "groom":
            return run_groom(args, store, secrets or KeychainStore(), http)
        if args.command == "dedupe":
            return run_dedupe(args, store, secrets or KeychainStore(), http)
        if args.command == "kb":
            return run_kb(args, store, secrets or KeychainStore(), http)
        if args.command == "jira":
            return run_jira(args, store, secrets or KeychainStore(), http)
        match args.action:
            case "list":
                last = store.last_used_name()
                _emit(
                    {
                        "last_used": last,
                        "profiles": [
                            {"name": p.name, "site_url": p.site_url, "projects": p.projects}
                            for p in store.list()
                        ],
                    }
                )
            case "show":
                _emit(store.get(args.name).model_dump(mode="json"))
            case "create":
                values = _field_values(args)
                profile = Profile(name=args.name, **values)
                problems = check_local(profile)
                if problems:
                    _emit({"ok": False, "problems": problems})
                    return 1
                store.save(profile, overwrite=args.overwrite)
                store.set_last_used(profile.name)
                _emit(profile.model_dump(mode="json"))
            case "update":
                current = store.get(args.name)
                values = _merge_defaults(current, _field_values(args))
                updated = with_overrides(current, **values)
                store.save(updated, overwrite=True)
                _emit(updated.model_dump(mode="json"))
            case "clone":
                source = store.get(args.source)
                values = _merge_defaults(source, _field_values(args))
                _emit(store.clone(args.source, args.new_name, **values).model_dump(mode="json"))
            case "use":
                store.set_last_used(args.name)
                _emit({"last_used": args.name})
            case "delete":
                store.delete(args.name)
                _emit({"deleted": args.name})
            case "export":
                preset = export_preset(store.get(args.name))
                if args.output:
                    Path(args.output).expanduser().write_text(json.dumps(preset, indent=2) + "\n")
                    _emit({"ok": True, "written": args.output})
                else:
                    _emit(preset)
            case "import":
                try:
                    data = json.loads(Path(args.file).expanduser().read_text())
                except (OSError, json.JSONDecodeError) as e:
                    raise ProfileError(f"cannot read the preset file: {e}") from None
                profile = import_preset(data, args.name, args.kb_path)
                problems = check_local(profile)
                if problems:
                    _emit({"ok": False, "problems": problems})
                    return 1
                store.save(profile, overwrite=args.overwrite)
                store.set_last_used(profile.name)
                _emit(profile.model_dump(mode="json"))
            case "check":
                problems = check_local(store.get(args.name))
                _emit({"ok": not problems, "problems": problems})
                return 0 if not problems else 1
    except httpx.HTTPError as e:
        print(f"network error: {type(e).__name__}: {e}", file=sys.stderr)
        return 5
    except oauth.AuthRequired as e:
        print(f"auth required: {e}", file=sys.stderr)
        return 3
    except (oauth.AuthError, CallbackError, SecretStoreError, token_login.TokenLoginError) as e:
        print(f"auth error: {e}", file=sys.stderr)
        return 3
    except ProfileError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"invalid input: {e}", file=sys.stderr)
        return 2
    return 0


def run_usage(args, home) -> int:
    if args.reset:
        usage.log_path(home).unlink(missing_ok=True)
        _emit({"ok": True, "reset": True})
        return 0
    summary = usage.summarize(usage.load_entries(home, args.days))
    static = usage.static_costs(PLUGIN_ROOT) if args.static else None
    if args.format == "json":
        _emit({**summary, **({"instructions": static} if static else {})})
    else:
        print(usage.render_table(summary, static))
    return 0


def main() -> int:
    """Run the CLI and record how much output it handed back (command name and sizes only)."""
    argv = sys.argv[1:]
    name = usage.command_name(argv)
    out, err = usage.CountingStream(sys.stdout), usage.CountingStream(sys.stderr)
    started = time.monotonic()
    with redirect_stdout(out), redirect_stderr(err):
        code = run(argv)
    if not name.startswith("usage"):
        usage.record(
            {
                "ts": int(time.time()),
                "command": name,
                "tokens": usage.estimate_tokens(out.chars + err.chars),
                "chars": out.chars + err.chars,
                "seconds": round(time.monotonic() - started, 2),
                "exit": code,
            }
        )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
