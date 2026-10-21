"""Guards for a plugin that is shared inside a company: nothing personal or company-specific ships.

The checks are patterns, not name lists (a list of names would itself be personal data in the repo).
"""

import re
from pathlib import Path

import pytest

from jira_agent.config import Profile

SELF = Path(__file__).resolve()
ROOT = SELF.parents[1]
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "dist", "node_modules"}
SKIP_FILES = {"uv.lock"}
TEXT_SUFFIXES = {".py", ".md", ".json", ".toml", ".txt", ".yml", ".yaml", ".cfg", ".gitignore"}

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
ALLOWED_EMAIL_DOMAINS = {"example.com", "example.org"}
HOME_PATH = re.compile(r"(?:/Users/|/home/|[A-Za-z]:\\Users\\)(?!<|\.\.\.|\$|\{|path|you|name)\w")
ATLASSIAN_SITE = re.compile(r"\b([A-Za-z0-9-]+)\.atlassian\.net\b")
PLACEHOLDER_SITES = {
    "acme",
    "other",
    "nope",
    "yoursite",
    "yourcompany",
    "example",
    "site",
    "your-site",
}
TOKEN_SHAPED = re.compile(r"ATATT[A-Za-z0-9_=-]{60,}|-----BEGIN [A-Z ]*PRIVATE KEY-----")


def repo_files():
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.name in SKIP_FILES or path.resolve() == SELF:
            continue  # this file holds deliberate fake examples to prove the patterns work
        if SKIP_DIRS & set(path.relative_to(ROOT).parts):
            continue
        if path.suffix in TEXT_SUFFIXES or path.name in {".gitignore"}:
            yield path


FILES = sorted(repo_files())


def placeholder_domain(domain: str) -> bool:
    domain = domain.lower()
    return any(domain == d or domain.endswith("." + d) for d in ALLOWED_EMAIL_DOMAINS)


def hits(pattern, group=None):
    found = []
    for path in FILES:
        for number, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            for match in pattern.finditer(line):
                found.append((path.relative_to(ROOT), number, match.group(group or 0)))
    return found


def test_the_scan_actually_covers_the_repo():
    names = {p.relative_to(ROOT).as_posix() for p in FILES}
    assert len(FILES) > 60
    assert {"README.md", "agents/jira-agent.md", "src/jira_agent/cli.py"} <= names


def test_only_placeholder_email_addresses_ship():
    bad = [h for h in hits(EMAIL, 1) if not placeholder_domain(h[2])]
    assert bad == [], f"real-looking email addresses found: {bad}"


def test_no_absolute_home_directory_paths_ship():
    assert hits(HOME_PATH) == []


def test_only_placeholder_atlassian_sites_ship():
    bad = [h for h in hits(ATLASSIAN_SITE, 1) if h[2].lower() not in PLACEHOLDER_SITES]
    assert bad == [], f"non-placeholder Atlassian sites found: {bad}"


def test_no_token_or_key_shaped_strings_ship():
    assert hits(TOKEN_SHAPED) == []


def test_the_scanner_itself_catches_what_it_should():
    assert EMAIL.search("write to jane.doe@corp.example.org now")
    assert placeholder_domain("git.example.com") and placeholder_domain("example.org")
    assert not placeholder_domain("corp-internal.net") and not placeholder_domain("notexample.com")
    assert HOME_PATH.search("see /Users/jane/project") and HOME_PATH.search(r"C:\Users\jane\x")
    assert not HOME_PATH.search("use /Users/<you>/x or /home/path/to/x")
    assert ATLASSIAN_SITE.search("https://mycorp.atlassian.net/browse/X-1").group(1) == "mycorp"
    assert TOKEN_SHAPED.search("ATATT" + "a" * 80)


def test_a_profile_has_no_default_site_projects_or_folder():
    required = {n for n, f in Profile.model_fields.items() if f.is_required()}
    assert {"name", "site_url", "projects", "kb_path"} <= required


@pytest.mark.parametrize("path", ["agents/jira-agent.md", ".claude-plugin/plugin.json"])
def test_plugin_metadata_carries_no_email_addresses(path):
    assert not EMAIL.search((ROOT / path).read_text())
