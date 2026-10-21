import json
import os
from pathlib import Path

import pytest

from jira_agent.auth.keychain import MemoryStore
from jira_agent.cli import run
from jira_agent.config import (
    PRESET_FIELDS,
    PRESET_FILE,
    PRESET_VERSION,
    Profile,
    ProfileError,
    ProfileStore,
    export_preset,
    find_preset,
    import_preset,
    suggested_name,
)

SITE = "https://acme.atlassian.net"


def make_profile(tmp_path, **kw):
    base = {
        "name": "team-a",
        "site_url": SITE,
        "projects": ["PROJ", "OPS"],
        "kb_path": str(tmp_path / "my-private-folder"),
        "sync_policy": "daily",
        "write_mode": "confirm_each",
        "dedupe_threshold": 0.7,
        "grooming_template": "acme",
        "issue_defaults": {
            "issue_type": "Story",
            "labels": ["web"],
            "components": [],
            "priority": None,
        },
    }
    return Profile(**{**base, **kw})


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("JIRA_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("JIRA_AGENT_PRESET", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path / "home"


# --- export -----------------------------------------------------------------------------------


def test_export_carries_the_shared_settings_and_nothing_personal(tmp_path):
    preset = export_preset(make_profile(tmp_path))
    assert preset["preset_version"] == PRESET_VERSION and preset["suggested_name"] == "team-a"
    assert set(preset) == {"preset_version", "suggested_name", *PRESET_FIELDS}
    text = json.dumps(preset)
    assert "my-private-folder" not in text and "kb_path" not in text
    assert preset["projects"] == ["PROJ", "OPS"] and preset["dedupe_threshold"] == 0.7


# --- import -----------------------------------------------------------------------------------


def test_import_builds_a_profile_with_a_default_private_folder(tmp_path):
    profile = import_preset(export_preset(make_profile(tmp_path)))
    assert profile.name == "team-a" and profile.kb_path == str(
        Path("~/jira-kb/team-a").expanduser()
    )
    assert profile.sync_policy == "daily" and profile.issue_defaults.labels == ["web"]


def test_import_lets_the_person_choose_their_name_and_folder(tmp_path):
    profile = import_preset(export_preset(make_profile(tmp_path)), "mine", str(tmp_path / "kb"))
    assert profile.name == "mine" and profile.kb_path == str(tmp_path / "kb")


def test_a_missing_suggested_name_comes_from_the_site():
    assert suggested_name("https://acme.atlassian.net") == "acme"
    assert suggested_name("https://Weird Host.example") == "jira"
    data = {"preset_version": 1, "site_url": SITE, "projects": ["PROJ"]}
    assert import_preset(data).name == "acme"


@pytest.mark.parametrize(
    "data,message",
    [
        ("not a dict", "not a jira-agent team preset"),
        ({"site_url": SITE, "projects": ["P"]}, "not a jira-agent team preset"),
        ({"preset_version": 99, "site_url": SITE, "projects": ["P"]}, "newer version"),
        ({"preset_version": 1, "projects": ["PROJ"]}, "missing: site_url"),
        ({"preset_version": 1, "site_url": SITE}, "missing: projects"),
    ],
)
def test_bad_presets_are_refused_with_a_clear_message(data, message):
    with pytest.raises(ProfileError, match=message):
        import_preset(data)


def test_values_in_a_preset_are_validated_like_any_profile():
    data = {"preset_version": 1, "site_url": "http://insecure.example", "projects": ["PROJ"]}
    with pytest.raises(ValueError, match="https"):
        import_preset(data)
    data = {"preset_version": 1, "site_url": SITE, "projects": ["bad key"]}
    with pytest.raises(ValueError, match="invalid project key"):
        import_preset(data)


# --- discovery --------------------------------------------------------------------------------


def test_find_preset_prefers_the_env_var_then_this_folder_then_the_agent_home(
    home, tmp_path, monkeypatch
):
    assert find_preset(home=home) is None
    (home).mkdir(parents=True)
    in_home = home / "preset.json"
    in_home.write_text("{}")
    assert find_preset(home=home) == in_home
    here = tmp_path / PRESET_FILE
    here.write_text("{}")
    assert find_preset(home=home) == here
    explicit = tmp_path / "elsewhere.json"
    explicit.write_text("{}")
    monkeypatch.setenv("JIRA_AGENT_PRESET", str(explicit))
    assert find_preset(home=home) == explicit
    monkeypatch.setenv("JIRA_AGENT_PRESET", str(tmp_path / "missing.json"))
    assert find_preset(home=home) == here


# --- through the CLI --------------------------------------------------------------------------


def test_admin_exports_and_a_colleague_imports(home, tmp_path, capsys):
    admin = ProfileStore(tmp_path / "admin-home")
    admin.save(make_profile(tmp_path))
    out_file = tmp_path / "team.json"
    assert run(["profile", "export", "team-a", "--output", str(out_file)], admin) == 0
    capsys.readouterr()

    colleague = ProfileStore(tmp_path / "colleague-home")
    kb = tmp_path / "colleague-kb"
    assert run(["profile", "import", str(out_file), "--kb-path", str(kb)], colleague) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["name"] == "team-a" and saved["kb_path"] == str(kb)
    assert colleague.last_used_name() == "team-a"
    for field in PRESET_FIELDS:
        assert (
            colleague.get("team-a").model_dump(mode="json")[field]
            == json.loads(out_file.read_text())[field]
        )


def test_export_prints_when_no_file_is_given(home, tmp_path, capsys):
    store = ProfileStore(tmp_path / "h")
    store.save(make_profile(tmp_path))
    assert run(["profile", "export", "team-a"], store) == 0
    assert json.loads(capsys.readouterr().out)["preset_version"] == PRESET_VERSION


def test_import_refuses_to_overwrite_unless_told(home, tmp_path):
    store = ProfileStore(tmp_path / "h")
    preset = tmp_path / "p.json"
    preset.write_text(json.dumps(export_preset(make_profile(tmp_path))))
    kb = str(tmp_path / "kb")
    assert run(["profile", "import", str(preset), "--kb-path", kb], store) == 0
    assert run(["profile", "import", str(preset), "--kb-path", kb], store) == 2
    assert run(["profile", "import", str(preset), "--kb-path", kb, "--overwrite"], store) == 0


@pytest.mark.parametrize("content", ["{not json", "[]", '{"preset_version": 1}'])
def test_import_of_a_broken_file_exits_cleanly(home, tmp_path, content):
    preset = tmp_path / "p.json"
    preset.write_text(content)
    assert run(["profile", "import", str(preset)], ProfileStore(tmp_path / "h")) == 2
    assert (
        run(["profile", "import", str(tmp_path / "nope.json")], ProfileStore(tmp_path / "h")) == 2
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX path /proc")
def test_import_rejects_an_unwritable_folder(home, tmp_path, capsys):
    preset = tmp_path / "p.json"
    preset.write_text(json.dumps(export_preset(make_profile(tmp_path))))
    code = run(
        ["profile", "import", str(preset), "--kb-path", "/proc/nope/kb"],
        ProfileStore(tmp_path / "h"),
    )
    assert code == 1 and "not writable" in capsys.readouterr().out


def test_session_check_points_a_new_person_at_the_team_preset(home, tmp_path, capsys):
    (tmp_path / PRESET_FILE).write_text(json.dumps(export_preset(make_profile(tmp_path))))
    assert run(["session", "check", "--offline"], ProfileStore(home), MemoryStore()) == 0
    out = json.loads(capsys.readouterr().out)
    step = out["next"][0]
    assert step["step"] == "jira-setup" and "profile import" in step["command"]
    assert out["preset"].endswith(PRESET_FILE) and "team preset" in step["reason"]


def test_session_check_without_a_preset_asks_to_create_a_profile(home, capsys):
    assert run(["session", "check", "--offline"], ProfileStore(home), MemoryStore()) == 0
    out = json.loads(capsys.readouterr().out)
    assert "profile create" in out["next"][0]["command"] and "preset" not in out


def test_an_existing_profile_is_not_second_guessed_by_a_preset(home, tmp_path, capsys):
    store = ProfileStore(home)
    store.save(make_profile(tmp_path))
    store.set_last_used("team-a")
    (tmp_path / PRESET_FILE).write_text(json.dumps(export_preset(make_profile(tmp_path))))
    assert run(["session", "check", "--offline"], store, MemoryStore()) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["active"] == "team-a" and "preset" not in out
