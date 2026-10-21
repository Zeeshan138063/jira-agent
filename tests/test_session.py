import json
import re
import time
from pathlib import Path

import pytest
import respx

from jira_agent.auth import oauth
from jira_agent.auth.keychain import MemoryStore, token_key
from jira_agent.cli import run
from jira_agent.config import Profile, ProfileStore
from jira_agent.kb.store import KbStore

SITE = "https://acme.atlassian.net"
ROOT = Path(__file__).resolve().parents[1]
MYSELF = f"{oauth.API_BASE}/cloud-1/rest/api/3/myself"


def steps(out):
    return [s["step"] for s in out["next"]]


def check(store, secrets, *extra):
    return run(["session", "check", *extra], store, secrets)


@pytest.fixture
def store(tmp_path):
    return ProfileStore(tmp_path / "home")


@pytest.fixture
def secrets():
    return MemoryStore()


def add_profile(store, tmp_path, **kw):
    store.save(
        Profile(
            name="p",
            site_url=SITE,
            projects=["PROJ"],
            kb_path=str(tmp_path / "kb"),
            **kw,
        )
    )
    store.set_last_used("p")


def sign_in(secrets):
    oauth.save_client(secrets, "cid", "csecret", 0)
    secrets.set(
        token_key(SITE),
        {
            "access_token": "t",
            "refresh_token": "r",
            "expires_at": time.time() + 3600,
            "cloud_id": "cloud-1",
            "site_url": SITE,
        },
    )


def test_no_profile_points_to_setup(store, secrets, capsys):
    assert check(store, secrets) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ready"] is False and steps(out) == ["jira-setup"] and out["active"] is None
    assert out["plugin_root"] == str(ROOT)


def test_unknown_profile_name(store, secrets, capsys):
    check(store, secrets, "--profile", "ghost")
    out = json.loads(capsys.readouterr().out)
    assert steps(out) == ["jira-setup"] and "no such profile" in out["next"][0]["reason"]


def test_not_signed_in_asks_for_login_without_any_app_setup(store, secrets, tmp_path, capsys):
    add_profile(store, tmp_path)
    check(store, secrets)
    out = json.loads(capsys.readouterr().out)
    assert out["ready"] is False and steps(out)[0] == "auth-login"
    assert "API token" in out["next"][0]["reason"]
    assert out["auth"] == {
        "method": None,
        "oauth_app_configured": False,
        "signed_in": False,
        "account": None,
    }


@respx.mock
def test_not_signed_in_asks_for_login(store, secrets, tmp_path, capsys):
    add_profile(store, tmp_path)
    oauth.save_client(secrets, "cid", "csecret", 0)
    check(store, secrets)
    out = json.loads(capsys.readouterr().out)
    assert steps(out)[0] == "auth-login" and out["ready"] is False
    assert out["auth"]["oauth_app_configured"] is True and out["auth"]["signed_in"] is False


@respx.mock
def test_ready_with_pending_kb_backfill_is_non_blocking(store, secrets, tmp_path, capsys):
    add_profile(store, tmp_path)
    sign_in(secrets)
    respx.get(MYSELF).respond(json={"accountId": "u1", "displayName": "Pat"})
    check(store, secrets)
    out = json.loads(capsys.readouterr().out)
    assert out["ready"] is True and out["auth"]["account"]["display_name"] == "Pat"
    kb_step = out["next"][0]
    assert kb_step["step"] == "kb-sync" and kb_step["blocking"] is False
    assert kb_step["command"].endswith("--project PROJ")


@respx.mock
def test_fully_current_session_has_nothing_to_do(store, secrets, tmp_path, capsys):
    add_profile(store, tmp_path, sync_policy="manual")
    sign_in(secrets)
    with KbStore(tmp_path / "kb") as kb:
        kb.bind_site(SITE)
        kb.set_meta("synced_at:PROJ", str(int(time.time())))
    respx.get(MYSELF).respond(json={"accountId": "u1"})
    check(store, secrets)
    out = json.loads(capsys.readouterr().out)
    assert out["ready"] is True and out["next"] == []
    assert out["kb"]["projects"][0]["recommended_action"] == "none"


def test_offline_skips_the_network(store, secrets, tmp_path, capsys):
    add_profile(store, tmp_path)
    sign_in(secrets)
    check(store, secrets, "--offline")
    out = json.loads(capsys.readouterr().out)
    assert out["auth"]["signed_in"] is None and out["ready"] is True


def test_kb_bound_to_another_site_blocks(store, secrets, tmp_path, capsys):
    add_profile(store, tmp_path)
    oauth.save_client(secrets, "cid", "csecret", 0)
    with KbStore(tmp_path / "kb") as kb:
        kb.bind_site("https://other.atlassian.net")
    check(store, secrets, "--offline")
    out = json.loads(capsys.readouterr().out)
    assert any("belongs to" in s["reason"] for s in out["next"])
    assert out["ready"] is False


def test_agent_file_is_well_formed_and_routes_every_skill():
    text = (ROOT / "agents" / "jira-agent.md").read_text()
    front = re.match(r"---\n(.*?)\n---\n", text, re.S).group(1)
    assert "name: jira-agent" in front and "description:" in front and "tools:" in front
    for skill in (ROOT / "skills").iterdir():
        assert skill.name in text, f"agent does not mention {skill.name}"


def test_every_skill_has_frontmatter_matching_its_folder():
    for skill in (ROOT / "skills").iterdir():
        body = (skill / "SKILL.md").read_text()
        front = re.match(r"---\nname: (.+)\ndescription: (.+)\n---\n", body)
        assert front and front.group(1) == skill.name, skill.name


def test_plugin_manifest_points_at_real_paths():
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    for agent in manifest["agents"]:
        assert (ROOT / agent).is_file()
    assert (ROOT / manifest["skills"]).is_dir()
