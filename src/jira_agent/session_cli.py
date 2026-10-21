import json
from pathlib import Path

from jira_agent.auth import credentials, oauth
from jira_agent.auth.keychain import CLIENT_KEY, SecretStoreError, token_key
from jira_agent.config import Profile, ProfileError, ProfileStore, check_local, find_preset
from jira_agent.kb.status import kb_status
from jira_agent.kb.store import KbError, KbStore

PLUGIN_ROOT = Path(__file__).resolve().parents[2]


def _emit(data) -> None:
    print(json.dumps(data, indent=2))


def add_parsers(sub) -> None:
    session = sub.add_parser("session", help="session start-up checks")
    ssub = session.add_subparsers(dest="action", required=True)
    p = ssub.add_parser("check", help="profile, sign-in and knowledge-base status in one call")
    p.add_argument("--profile", help="profile name (default: last used)")
    p.add_argument("--offline", action="store_true", help="skip the live Jira identity check")


def _step(step: str, command: str, reason: str, blocking: bool = True) -> dict:
    return {"step": step, "command": command, "reason": reason, "blocking": blocking}


def _auth(profile: Profile, secrets, http, offline: bool) -> tuple[dict, list[dict]]:
    try:
        record = secrets.get(token_key(profile.site_url))
        client_configured = bool(secrets.get(CLIENT_KEY))
    except SecretStoreError as e:
        return {"method": None, "signed_in": False, "detail": str(e)}, [
            _step("fix-keychain", "", str(e))
        ]
    method = credentials.method_of(record)
    auth = {
        "method": method,
        "oauth_app_configured": client_configured,
        "signed_in": False,
        "account": None,
    }
    if not record:
        return auth, [
            _step(
                "auth-login",
                "jira-agent auth login",
                "not signed in; a browser page opens to connect with an Atlassian API token",
            )
        ]
    expiry = credentials.token_expiry(record)
    auth["expiry"] = expiry
    renew = credentials.expiry_message(expiry)
    renew_steps = (
        [_step("auth-renew", "jira-agent auth login", renew, blocking=False)] if renew else []
    )
    if offline:
        return {**auth, "signed_in": None}, renew_steps
    try:
        me = credentials.preflight(profile.site_url, secrets, http)
    except oauth.AuthError as e:
        auth["detail"] = str(e)
        return auth, [_step("auth-login", "jira-agent auth login", str(e))]
    return {**auth, "signed_in": True, "account": me}, renew_steps


def run_session(args, store: ProfileStore, secrets, http) -> int:
    profiles = store.list()
    last = store.last_used_name()
    wanted = args.profile or last
    result: dict = {
        "plugin_root": str(PLUGIN_ROOT),
        "profiles": [
            {"name": p.name, "site_url": p.site_url, "projects": p.projects} for p in profiles
        ],
        "last_used": last,
        "active": None,
        "profile": None,
        "local_problems": [],
        "auth": None,
        "kb": None,
        "ready": False,
        "next": [],
    }
    if not wanted:
        preset = find_preset(home=store.home)
        if preset:
            result["preset"] = str(preset)
            result["next"] = [
                _step(
                    "jira-setup",
                    f'jira-agent profile import "{preset}"',
                    "a team preset was found; importing it only asks where to keep your ticket "
                    "copy",
                )
            ]
        else:
            result["next"] = [
                _step("jira-setup", "jira-agent profile create <name> ...", "no saved profile yet")
            ]
        _emit(result)
        return 0
    try:
        profile = store.get(wanted)
    except ProfileError as e:
        result["next"] = [_step("jira-setup", "jira-agent profile list", str(e))]
        _emit(result)
        return 0

    result["active"] = profile.name
    result["profile"] = profile.model_dump(mode="json")
    result["local_problems"] = check_local(profile)
    steps = [
        _step("jira-setup", f"jira-agent profile update {profile.name} ...", p)
        for p in result["local_problems"]
    ]

    result["auth"], auth_steps = _auth(profile, secrets, http, args.offline)
    steps += auth_steps

    if not result["local_problems"]:
        try:
            with KbStore(profile.kb_path) as kb:
                kb.bind_site(profile.site_url)
                result["kb"] = kb_status(kb, profile)
        except KbError as e:
            steps.append(_step("jira-setup", "jira-agent profile update", str(e)))
    for project in (result["kb"] or {}).get("projects", []):
        if project["recommended_action"] != "none":
            steps.append(
                _step(
                    "kb-sync",
                    f"jira-agent kb sync --project {project['project']}",
                    f"{project['project']}: {project['reason']}",
                    blocking=False,
                )
            )

    result["next"] = steps
    result["ready"] = not any(s["blocking"] for s in steps) and (
        result["auth"]["signed_in"] is not False
    )
    _emit(result)
    return 0
