from collections.abc import Iterator

from jira_agent.client.adf import from_adf, to_adf
from jira_agent.client.gate import WriteGate
from jira_agent.client.http import JiraClient

SUMMARY_FIELDS = [
    "summary",
    "description",
    "status",
    "issuetype",
    "priority",
    "assignee",
    "reporter",
    "labels",
    "components",
    "parent",
    "created",
    "updated",
    "issuelinks",
    "fixVersions",
]
AUTO_FIELDS = {"project", "issuetype", "reporter"}


class IssueError(Exception):
    pass


def _name(v) -> str | None:
    return v.get("name") or v.get("displayName") if isinstance(v, dict) else v


def normalize_issue(raw: dict) -> dict:
    f = raw.get("fields", {})
    return {
        "id": raw.get("id"),
        "key": raw.get("key"),
        "summary": f.get("summary"),
        "type": _name(f.get("issuetype")),
        "status": _name(f.get("status")),
        "status_category": ((f.get("status") or {}).get("statusCategory") or {}).get("key"),
        "priority": _name(f.get("priority")),
        "assignee": _name(f.get("assignee")),
        "reporter": _name(f.get("reporter")),
        "labels": f.get("labels") or [],
        "components": [c.get("name") for c in f.get("components") or []],
        "parent": (f.get("parent") or {}).get("key"),
        "description": from_adf(f.get("description")),
        "created": f.get("created"),
        "updated": f.get("updated"),
        "links": [_link(link) for link in f.get("issuelinks") or []],
    }


def _link(link: dict) -> dict:
    other = link.get("outwardIssue") or link.get("inwardIssue") or {}
    direction = "outward" if "outwardIssue" in link else "inward"
    return {
        "type": link.get("type", {}).get(direction),
        "key": other.get("key"),
        "summary": other.get("fields", {}).get("summary"),
    }


def required_fields(meta_fields: list[dict]) -> list[dict]:
    return [
        {
            "key": f["fieldId"],
            "name": f.get("name"),
            "type": f.get("schema", {}).get("type"),
            "allowed": [_name(a) or a.get("value") for a in f.get("allowedValues") or []],
        }
        for f in meta_fields
        if f.get("required") and f["fieldId"] not in AUTO_FIELDS and not f.get("hasDefaultValue")
    ]


def coerce_value(field: dict, raw):
    schema = field.get("schema", {})
    kind, item = schema.get("type"), schema.get("items")
    if not isinstance(raw, str):
        return raw
    if kind == "array":
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if item == "string":
            return parts
        key = "name" if item in ("component", "version") else "value"
        return [{key: p} for p in parts]
    if kind == "user":
        return {"accountId": raw}
    if kind == "priority":
        return {"name": raw}
    if kind == "option":
        return {"value": raw}
    if kind == "issuelink":
        return {"key": raw}
    if schema.get("system") == "parent":
        return {"key": raw}
    if kind == "doc" or schema.get("system") == "description":
        return to_adf(raw)
    if kind == "number":
        return float(raw)
    return raw


class IssueAPI:
    def __init__(self, client: JiraClient, gate: WriteGate):
        self.client = client
        self.gate = gate

    def get_issue(self, key: str, with_comments: bool = True) -> dict:
        issue = normalize_issue(self.client.get(f"issue/{key}", fields=",".join(SUMMARY_FIELDS)))
        if with_comments:
            body = self.client.get(f"issue/{key}/comment", maxResults=100)
            issue["comments"] = [
                {
                    "author": _name(c.get("author")),
                    "created": c.get("created"),
                    "body": from_adf(c.get("body")),
                }
                for c in body.get("comments", [])
            ]
        return issue

    def search(
        self,
        jql: str,
        fields: list[str] | None = None,
        limit: int | None = None,
        expand: str | None = None,
    ) -> Iterator[dict]:
        token, seen = None, 0
        while True:
            body = {
                "jql": jql,
                "fields": fields or SUMMARY_FIELDS,
                "maxResults": min(100, limit - seen) if limit else 100,
            }
            if expand:
                body["expand"] = expand
            if token:
                body["nextPageToken"] = token
            page = self.client.post("search/jql", body)
            for raw in page.get("issues", []):
                yield raw
                seen += 1
                if limit and seen >= limit:
                    return
            token = page.get("nextPageToken")
            if not token or page.get("isLast"):
                return

    def issue_types(self, project: str) -> list[dict]:
        out, start = [], 0
        while True:
            page = self.client.get(
                f"issue/createmeta/{project}/issuetypes", startAt=start, maxResults=50
            )
            values = page.get("issueTypes") or page.get("values") or []
            out += [
                {"id": v["id"], "name": v["name"], "subtask": v.get("subtask", False)}
                for v in values
            ]
            start += len(values)
            if not values or start >= page.get("total", 0):
                return out

    def create_meta(self, project: str, issue_type: str) -> dict:
        types = self.issue_types(project)
        match = next(
            (t for t in types if t["name"].lower() == issue_type.lower() or t["id"] == issue_type),
            None,
        )
        if not match:
            names = ", ".join(t["name"] for t in types)
            raise IssueError(f"issue type {issue_type!r} not in {project}; available: {names}")
        fields, start = [], 0
        while True:
            page = self.client.get(
                f"issue/createmeta/{project}/issuetypes/{match['id']}",
                startAt=start,
                maxResults=100,
            )
            values = page.get("fields") or page.get("values") or []
            fields += values
            start += len(values)
            if not values or start >= page.get("total", 0):
                break
        return {"issue_type": match, "fields": fields, "required": required_fields(fields)}

    def build_create_fields(
        self, project: str, meta: dict, summary: str, description: str | None, extra: dict
    ) -> dict:
        by_id = {f["fieldId"]: f for f in meta["fields"]}
        fields: dict = {
            "project": {"key": project},
            "issuetype": {"id": meta["issue_type"]["id"]},
            "summary": summary,
        }
        if description:
            fields["description"] = to_adf(description)
        for key, raw in extra.items():
            if key not in by_id:
                raise IssueError(f"field {key!r} is not available on this issue type")
            fields[key] = coerce_value(by_id[key], raw)
        missing = [r for r in meta["required"] if r["key"] not in fields]
        if missing:
            detail = ", ".join(f"{m['key']} ({m['name']})" for m in missing)
            raise IssueError(f"missing required field(s): {detail}")
        return fields

    def prepare_create(
        self,
        project: str,
        issue_type: str,
        summary: str,
        description: str | None = None,
        extra: dict | None = None,
        defaults: dict | None = None,
        meta: dict | None = None,
    ) -> tuple[dict, dict]:
        meta = meta or self.create_meta(project, issue_type)
        available = {f["fieldId"] for f in meta["fields"]}
        merged = {k: v for k, v in (defaults or {}).items() if k in available and v}
        merged.update(extra or {})
        return meta, self.build_create_fields(project, meta, summary, description, merged)

    def create_issue(
        self,
        project: str,
        issue_type: str,
        summary: str,
        description: str | None = None,
        extra: dict | None = None,
        defaults: dict | None = None,
        meta: dict | None = None,
    ):
        meta, fields = self.prepare_create(
            project, issue_type, summary, description, extra, defaults, meta
        )
        preview = {
            "target": project,
            "fields": {**fields, "description": description} if description else fields,
        }
        return self.gate.run(
            f"create {meta['issue_type']['name']} in {project}",
            preview,
            lambda: self.client.post("issue", {"fields": fields}),
        )

    def update_issue(
        self,
        key: str,
        summary: str | None = None,
        description: str | None = None,
        extra: dict | None = None,
    ):
        editmeta = self.client.get(f"issue/{key}/editmeta").get("fields", {})
        current = self.client.get(f"issue/{key}", fields=",".join(SUMMARY_FIELDS))["fields"]
        fields: dict = {}
        changes: dict = {}
        wanted = dict(extra or {})
        if summary is not None:
            wanted["summary"] = summary
        for field_key, raw in wanted.items():
            if field_key not in editmeta:
                raise IssueError(f"field {field_key!r} is not editable on {key}")
            fields[field_key] = coerce_value(editmeta[field_key] | {"fieldId": field_key}, raw)
            changes[field_key] = {"before": _plain(current.get(field_key)), "after": raw}
        if description is not None:
            if "description" not in editmeta:
                raise IssueError(f"description is not editable on {key}")
            fields["description"] = to_adf(description)
            changes["description"] = {
                "before": from_adf(current.get("description")),
                "after": description,
            }
        if not fields:
            raise IssueError("nothing to update")
        return self.gate.run(
            f"update {key}",
            {"target": key, "fields": changes},
            lambda: self.client.put(f"issue/{key}", {"fields": fields}) or {"key": key},
        )

    def transitions(self, key: str) -> list[dict]:
        body = self.client.get(f"issue/{key}/transitions")
        return [
            {"id": t["id"], "name": t["name"], "to": t.get("to", {}).get("name")}
            for t in body.get("transitions", [])
        ]

    def transition(self, key: str, to: str):
        options = self.transitions(key)
        match = next(
            (t for t in options if to.lower() in (t["name"].lower(), (t["to"] or "").lower())),
            None,
        )
        if not match:
            names = ", ".join(f"{t['name']} -> {t['to']}" for t in options)
            raise IssueError(f"no transition {to!r} for {key}; available: {names}")
        return self.gate.run(
            f"transition {key} via '{match['name']}'",
            {"target": key, "fields": {"transition": match}},
            lambda: (
                self.client.post(f"issue/{key}/transitions", {"transition": {"id": match["id"]}})
                or {"key": key}
            ),
        )

    def add_comment(self, key: str, body: str):
        return self.gate.run(
            f"comment on {key}",
            {"target": key, "fields": {"comment": body}},
            lambda: self.client.post(f"issue/{key}/comment", {"body": to_adf(body)}),
        )

    def link_types(self) -> list[dict]:
        return [
            {"name": t["name"], "inward": t["inward"], "outward": t["outward"]}
            for t in self.client.get("issueLinkType").get("issueLinkTypes", [])
        ]

    def link(self, link_type: str, from_key: str, to_key: str):
        """`link Blocks A B` makes A block B (A <outward verb> B).

        Jira's API names the sides confusingly: the issue sent as `inwardIssue` is the one that
        gets the outward verb ("blocks") and `outwardIssue` is the one it points at. Verified
        against a live site: the reverse mapping produced "A is blocked by B".
        """
        names = {t["name"].lower(): t["name"] for t in self.link_types()}
        if link_type.lower() not in names:
            raise IssueError(
                f"unknown link type {link_type!r}; available: {', '.join(names.values())}"
            )
        body = {
            "type": {"name": names[link_type.lower()]},
            "inwardIssue": {"key": from_key},
            "outwardIssue": {"key": to_key},
        }
        verb = next(t["outward"] for t in self.link_types() if t["name"] == body["type"]["name"])
        return self.gate.run(
            f"link {from_key} {verb} {to_key} ({body['type']['name']})",
            {"target": from_key, "fields": {"link": body}},
            lambda: self.client.post("issueLink", body) or {"key": from_key},
        )


def _plain(value):
    if isinstance(value, dict) and value.get("type") == "doc":
        return from_adf(value)
    if isinstance(value, dict):
        return _name(value)
    if isinstance(value, list):
        return [_name(v) if isinstance(v, dict) else v for v in value]
    return value
