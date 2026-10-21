import json
import os
import stat

import pytest

from jira_agent.cli import run
from jira_agent.config import Profile, ProfileError, ProfileStore, check_local, with_overrides


def make_profile(tmp_path, **kw):
    base = {
        "name": "proj",
        "site_url": "https://Acme.atlassian.net/",
        "projects": ["proj", "ops", "PROJ"],
        "kb_path": str(tmp_path / "kb"),
    }
    return Profile(**{**base, **kw})


@pytest.fixture
def store(tmp_path):
    return ProfileStore(tmp_path / "home")


def test_normalizes_site_url_and_projects(tmp_path):
    p = make_profile(tmp_path)
    assert p.site_url == "https://acme.atlassian.net"
    assert p.projects == ["PROJ", "OPS"]


@pytest.mark.parametrize(
    "kw",
    [
        {"site_url": "http://acme.atlassian.net"},
        {"projects": []},
        {"projects": ["bad key"]},
        {"name": "../evil"},
        {"dedupe_threshold": 1.5},
    ],
)
def test_rejects_invalid_values(tmp_path, kw):
    with pytest.raises(ValueError):
        make_profile(tmp_path, **kw)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_round_trip_and_private_file_mode(store, tmp_path):
    p = make_profile(tmp_path)
    path = store.save(p)
    assert store.get("proj") == p
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_save_refuses_overwrite_by_default(store, tmp_path):
    store.save(make_profile(tmp_path))
    with pytest.raises(ProfileError):
        store.save(make_profile(tmp_path))
    store.save(make_profile(tmp_path, grooming_template="x"), overwrite=True)
    assert store.get("proj").grooming_template == "x"


def test_list_puts_last_used_first(store, tmp_path):
    for n in ("a", "b", "c"):
        store.save(make_profile(tmp_path, name=n))
    store.set_last_used("b")
    assert [p.name for p in store.list()] == ["b", "a", "c"]


def test_delete_clears_last_used(store, tmp_path):
    store.save(make_profile(tmp_path))
    store.set_last_used("proj")
    store.delete("proj")
    assert store.last_used_name() is None
    assert store.list() == []


def test_list_skips_corrupt_profiles(store, tmp_path):
    store.save(make_profile(tmp_path))
    (store.profiles_dir / "broken.json").write_text("{nope")
    assert [p.name for p in store.list()] == ["proj"]


def test_clone_with_changes(store, tmp_path):
    store.save(make_profile(tmp_path))
    new = store.clone("proj", "proj-ops", projects=["OPS"])
    assert new.projects == ["OPS"] and new.site_url == "https://acme.atlassian.net"


def test_overrides_do_not_mutate_saved_profile(store, tmp_path):
    p = make_profile(tmp_path)
    store.save(p)
    o = with_overrides(p, kb_path=str(tmp_path / "other"), write_mode="dry_run")
    assert o.write_mode == "dry_run"
    assert store.get("proj").write_mode == "confirm_each"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_check_local_flags_unwritable_kb(tmp_path):
    p = make_profile(tmp_path, kb_path="/proc/nope/kb")
    assert check_local(p)
    assert check_local(make_profile(tmp_path)) == []


def test_cli_create_list_update_use(store, tmp_path, capsys):
    kb = str(tmp_path / "kb")
    assert (
        run(
            [
                "profile",
                "create",
                "proj",
                "--site-url",
                "https://acme.atlassian.net",
                "--projects",
                "proj",
                "--kb-path",
                kb,
                "--labels",
                "a,b",
            ],
            store,
        )
        == 0
    )
    capsys.readouterr()
    assert run(["profile", "update", "proj", "--issue-type", "Story"], store) == 0
    capsys.readouterr()
    saved = store.get("proj")
    assert saved.issue_defaults.issue_type == "Story"
    assert saved.issue_defaults.labels == ["a", "b"]
    assert run(["profile", "list"], store) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["last_used"] == "proj" and out["profiles"][0]["projects"] == ["PROJ"]


def test_cli_missing_profile_exits_2(store, capsys):
    assert run(["profile", "show", "nope"], store) == 2
