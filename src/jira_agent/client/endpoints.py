import json
from pathlib import Path

import httpx

from jira_agent.config import default_home

SPEC_URL = "https://dac-static.atlassian.com/cloud/jira/platform/swagger-v3.v3.json"
METHODS = ("get", "post", "put", "patch", "delete")
PREFIXES = (
    "/rest/api/3/issue",
    "/rest/api/3/search",
    "/rest/api/3/project",
    "/rest/api/3/field",
    "/rest/api/3/issuetype",
    "/rest/api/3/priority",
    "/rest/api/3/status",
    "/rest/api/3/user",
    "/rest/api/3/issueLink",
    "/rest/api/3/jql",
    "/rest/api/3/filter",
    "/rest/api/3/label",
    "/rest/api/3/myself",
    "/rest/api/3/version",
    "/rest/api/3/component",
)

BUILTIN = [
    ("GET", "/issue/{key}", "Get an issue"),
    ("POST", "/issue", "Create an issue"),
    ("PUT", "/issue/{key}", "Edit an issue"),
    ("GET", "/issue/{key}/editmeta", "Fields editable on an issue"),
    ("GET", "/issue/{key}/transitions", "List available transitions"),
    ("POST", "/issue/{key}/transitions", "Transition an issue"),
    ("GET", "/issue/{key}/comment", "List comments"),
    ("POST", "/issue/{key}/comment", "Add a comment"),
    ("POST", "/search/jql", "Search issues with JQL (token pagination)"),
    ("GET", "/issue/createmeta/{project}/issuetypes", "Issue types creatable in a project"),
    ("GET", "/issue/createmeta/{project}/issuetypes/{id}", "Fields for creating an issue type"),
    ("GET", "/issueLinkType", "List link types"),
    ("POST", "/issueLink", "Link two issues"),
    ("GET", "/myself", "Current user"),
    ("GET", "/project/search", "List projects"),
    ("GET", "/field", "List all fields"),
]


def catalog_path(home: Path | None = None) -> Path:
    return (home or default_home()) / "catalog" / "jira-v3.json"


def builtin_catalog() -> list[dict]:
    return [
        {
            "method": m,
            "path": f"/rest/api/3{p}",
            "summary": s,
            "write": m != "GET" and p != "/search/jql",
            "source": "builtin",
        }
        for m, p, s in BUILTIN
    ]


def parse_spec(spec: dict) -> list[dict]:
    entries = []
    for path, ops in spec.get("paths", {}).items():
        if not path.startswith(PREFIXES):
            continue
        for method in METHODS:
            op = ops.get(method)
            if not op:
                continue
            entries.append(
                {
                    "method": method.upper(),
                    "path": path,
                    "operation_id": op.get("operationId"),
                    "summary": op.get("summary"),
                    "tags": op.get("tags", []),
                    "write": method != "get" and not path.endswith("/search/jql"),
                    "source": "openapi",
                }
            )
    return sorted(entries, key=lambda e: (e["path"], e["method"]))


def refresh_catalog(home: Path | None = None, http: httpx.Client | None = None) -> Path:
    http = http or httpx.Client(timeout=60, follow_redirects=True)
    resp = http.get(SPEC_URL)
    resp.raise_for_status()
    entries = parse_spec(resp.json())
    if not entries:
        raise ValueError("spec contained no matching endpoints")
    path = catalog_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=1))
    return path


def load_catalog(home: Path | None = None) -> list[dict]:
    path = catalog_path(home)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
    return builtin_catalog()


def search_catalog(entries: list[dict], text: str | None) -> list[dict]:
    if not text:
        return entries
    needle = text.lower()
    return [
        e
        for e in entries
        if needle in e["path"].lower()
        or needle in (e.get("summary") or "").lower()
        or needle in (e.get("operation_id") or "").lower()
    ]
