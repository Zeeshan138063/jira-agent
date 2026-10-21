import json
import os
import re
import tempfile
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,19}$")


class KbMode(StrEnum):
    FTS = "fts"
    FTS_EMBEDDINGS = "fts+embeddings"


class SyncPolicy(StrEnum):
    ON_START = "on_start"
    MANUAL = "manual"
    DAILY = "daily"


class WriteMode(StrEnum):
    CONFIRM_EACH = "confirm_each"
    DRY_RUN = "dry_run"


class IssueDefaults(BaseModel):
    issue_type: str = "Task"
    priority: str | None = None
    labels: list[str] = Field(default_factory=list)
    components: list[str] = Field(default_factory=list)


class Profile(BaseModel):
    name: str
    site_url: str
    projects: list[str]
    kb_path: str
    kb_mode: KbMode = KbMode.FTS
    sync_policy: SyncPolicy = SyncPolicy.ON_START
    issue_defaults: IssueDefaults = Field(default_factory=IssueDefaults)
    grooming_template: str = "default"
    dedupe_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    write_mode: WriteMode = WriteMode.CONFIRM_EACH

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError("name must be 1-64 chars of letters, digits, '.', '_' or '-'")
        return v

    @field_validator("site_url")
    @classmethod
    def _site_url(cls, v: str) -> str:
        parsed = urlparse(v.strip())
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("site_url must be an https URL such as https://acme.atlassian.net")
        return f"https://{parsed.netloc.lower()}"

    @field_validator("projects")
    @classmethod
    def _projects(cls, v: list[str]) -> list[str]:
        keys = [k.strip().upper() for k in v]
        if not keys:
            raise ValueError("at least one project key is required")
        bad = [k for k in keys if not PROJECT_KEY_RE.match(k)]
        if bad:
            raise ValueError(f"invalid project key(s): {', '.join(bad)}")
        return list(dict.fromkeys(keys))

    @field_validator("kb_path")
    @classmethod
    def _kb_path(cls, v: str) -> str:
        return str(Path(v).expanduser())


class ProfileError(Exception):
    pass


def private_dir(path: Path) -> None:
    created = not path.exists()
    path.mkdir(parents=True, exist_ok=True)
    if created:
        path.chmod(0o700)


def default_home() -> Path:
    return Path(os.environ.get("JIRA_AGENT_HOME", "~/.jira-agent")).expanduser()


class ProfileStore:
    def __init__(self, home: Path | None = None):
        self.home = home or default_home()
        self.profiles_dir = self.home / "profiles"
        self.state_file = self.home / "state.json"

    def _path(self, name: str) -> Path:
        if not NAME_RE.match(name):
            raise ProfileError(f"invalid profile name: {name!r}")
        return self.profiles_dir / f"{name}.json"

    def _atomic_write(self, path: Path, data: dict) -> None:
        private_dir(self.home)
        private_dir(path.parent)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _read_state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def last_used_name(self) -> str | None:
        return self._read_state().get("last_used")

    def set_last_used(self, name: str) -> None:
        if not self._path(name).exists():
            raise ProfileError(f"no such profile: {name}")
        self._atomic_write(self.state_file, {**self._read_state(), "last_used": name})

    def save(self, profile: Profile, *, overwrite: bool = False) -> Path:
        path = self._path(profile.name)
        if path.exists() and not overwrite:
            raise ProfileError(f"profile already exists: {profile.name}")
        self._atomic_write(path, profile.model_dump(mode="json"))
        return path

    def get(self, name: str) -> Profile:
        path = self._path(name)
        try:
            return Profile.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ProfileError(f"no such profile: {name}") from None
        except ValueError as e:
            raise ProfileError(f"profile {name} is invalid: {e}") from e

    def list(self) -> list[Profile]:
        if not self.profiles_dir.exists():
            return []
        profiles = []
        for p in sorted(self.profiles_dir.glob("*.json")):
            try:
                profiles.append(self.get(p.stem))
            except ProfileError:
                continue
        last = self.last_used_name()
        profiles.sort(key=lambda pr: pr.name != last)
        return profiles

    def delete(self, name: str) -> None:
        path = self._path(name)
        if not path.exists():
            raise ProfileError(f"no such profile: {name}")
        path.unlink()
        if self.last_used_name() == name:
            state = self._read_state()
            state.pop("last_used", None)
            self._atomic_write(self.state_file, state)

    def clone(self, source: str, new_name: str, **changes) -> Profile:
        new = with_overrides(self.get(source), name=new_name, **changes)
        self.save(new)
        return new


def with_overrides(profile: Profile, **changes) -> Profile:
    changes = {k: v for k, v in changes.items() if v is not None}
    return Profile.model_validate({**profile.model_dump(), **changes})


PRESET_VERSION = 1
PRESET_FIELDS = (
    "site_url",
    "projects",
    "kb_mode",
    "sync_policy",
    "issue_defaults",
    "grooming_template",
    "dedupe_threshold",
    "write_mode",
)
PRESET_FILE = "jira-agent.preset.json"


def suggested_name(site_url: str) -> str:
    host = site_url.split("://", 1)[-1].split(".", 1)[0]
    return host if NAME_RE.match(host) else "jira"


def export_preset(profile: Profile) -> dict:
    """The shareable part of a profile: no secrets, no personal folder, no name."""
    data = profile.model_dump(mode="json")
    return {
        "preset_version": PRESET_VERSION,
        "suggested_name": profile.name,
        **{k: data[k] for k in PRESET_FIELDS},
    }


def import_preset(data, name: str | None = None, kb_path: str | None = None) -> Profile:
    if not isinstance(data, dict) or data.get("preset_version") != PRESET_VERSION:
        raise ProfileError("this is not a jira-agent team preset (or it is from a newer version)")
    missing = [k for k in ("site_url", "projects") if not data.get(k)]
    if missing:
        raise ProfileError(f"the preset is missing: {', '.join(missing)}")
    chosen = name or data.get("suggested_name") or suggested_name(data["site_url"])
    fields = {k: data[k] for k in PRESET_FIELDS if k in data}
    return Profile(name=chosen, kb_path=kb_path or f"~/jira-kb/{chosen}", **fields)


def find_preset(cwd: Path | None = None, home: Path | None = None) -> Path | None:
    """A team preset left for this person: the env var, then this folder, then the agent home."""
    explicit = os.environ.get("JIRA_AGENT_PRESET")
    candidates = [Path(explicit).expanduser()] if explicit else []
    candidates += [(cwd or Path.cwd()) / PRESET_FILE, (home or default_home()) / "preset.json"]
    return next((c for c in candidates if c.is_file()), None)


def check_local(profile: Profile) -> list[str]:
    problems = []
    kb = Path(profile.kb_path)
    probe = kb
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not probe.is_dir() or not os.access(probe, os.W_OK):
        problems.append(f"kb_path is not writable: {kb}")
    return problems
