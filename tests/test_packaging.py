"""The package is what teammates install; these checks stop its metadata from drifting."""

import json
import re
import struct
import tomllib
from pathlib import Path

import pytest

import jira_agent

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
MARKET = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def test_one_version_everywhere():
    versions = {
        "plugin.json": PLUGIN["version"],
        "marketplace.json": MARKET["plugins"][0]["version"],
        "pyproject.toml": PYPROJECT["project"]["version"],
        "__init__": jira_agent.__version__,
    }
    assert len(set(versions.values())) == 1, versions
    assert SEMVER.match(PLUGIN["version"])


def test_the_changelog_leads_with_the_current_version():
    first = re.search(r"^## (\d+\.\d+\.\d+)", (ROOT / "CHANGELOG.md").read_text(), re.M)
    assert first and first.group(1) == PLUGIN["version"]


def test_license_is_mit_and_consistent_everywhere():
    text = (ROOT / "LICENSE").read_text()
    assert text.startswith("MIT License") and "Permission is hereby granted" in text
    assert re.search(r"Copyright \(c\) \d{4} [A-Za-z ]+\n", text)
    assert PLUGIN["license"] == PYPROJECT["project"]["license"] == "MIT"


def test_author_is_a_neutral_name_not_an_address():
    name = PLUGIN["author"]["name"]
    assert name and "@" not in name and set(PLUGIN["author"]) == {"name"}
    assert MARKET["owner"]["name"] == name
    assert PYPROJECT["project"]["authors"][0]["name"] == name


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    assert data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


def test_icon_is_a_square_png_within_the_directory_limits():
    icon = ROOT / PLUGIN["icon"]
    width, height = png_size(icon)
    assert width == height and 512 <= width <= 2048
    assert icon.stat().st_size < 2_000_000


def test_marketplace_points_at_this_plugin():
    entry = MARKET["plugins"][0]
    assert entry["name"] == PLUGIN["name"] and entry["source"] in ("./", ".")
    target = (ROOT / entry["source"]).resolve()
    assert (target / ".claude-plugin" / "plugin.json").is_file()
    assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", MARKET["name"])


def test_declared_agent_and_skills_exist():
    for agent in PLUGIN["agents"]:
        assert (ROOT / agent).is_file()
    skills = sorted(p.name for p in (ROOT / PLUGIN["skills"]).iterdir() if p.is_dir())
    assert len(skills) == 8 and all((ROOT / "skills" / s / "SKILL.md").is_file() for s in skills)


def test_the_launcher_is_executable_and_runs_through_uv():
    launcher = ROOT / "bin" / "jira-agent"
    text = launcher.read_text()
    assert text.startswith("#!/bin/sh") and launcher.stat().st_mode & 0o111
    assert "exec uv run" in text and '"$@"' in text
    assert "/Users/" not in text and "/home/" not in text


@pytest.mark.parametrize(
    "path", sorted((ROOT / "skills").glob("*/SKILL.md")) + [ROOT / "agents" / "jira-agent.md"]
)
def test_instructions_use_the_launcher_not_a_hand_built_path(path):
    text = path.read_text()
    assert "uv run --project" not in text and "<plugin-root>" not in text
    assert "jira-agent" in text


def test_the_agent_tells_the_model_to_run_one_plain_command_per_call():
    text = (ROOT / "agents" / "jira-agent.md").read_text()
    assert "one plain command per call" in text and "no `cd`" in text


def test_the_environment_can_be_rebuilt_from_the_lock_file():
    assert (ROOT / "uv.lock").is_file()
    assert PYPROJECT["project"]["requires-python"].startswith(">=3.")
    assert "jira-agent" in PYPROJECT["project"]["scripts"]


def test_runtime_data_and_caches_are_not_tracked():
    ignore = (ROOT / ".gitignore").read_text()
    for name in (".venv/", "__pycache__/", ".env"):
        assert name in ignore


def test_docs_for_installing_and_releasing_exist_and_are_linked():
    readme = (ROOT / "README.md").read_text()
    for doc in ("INSTALL.md", "RELEASING.md", "PM_GUIDE.md", "TEAM_SETUP.md", "TOKEN_USAGE.md"):
        assert (ROOT / "docs" / doc).is_file() and f"docs/{doc}" in readme
    assert "claude plugin install jira-agent --marketplace" in readme
