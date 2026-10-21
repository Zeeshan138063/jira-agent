import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from jira_agent.config import private_dir

SCHEMA_VERSION = 2
DB_NAME = "index.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    jira_id TEXT,
    key TEXT NOT NULL UNIQUE,
    project TEXT NOT NULL,
    summary TEXT,
    type TEXT,
    status TEXT,
    priority TEXT,
    assignee TEXT,
    reporter TEXT,
    parent TEXT,
    labels TEXT,
    components TEXT,
    description TEXT,
    created TEXT,
    updated TEXT,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tickets_project ON tickets(project);
CREATE UNIQUE INDEX IF NOT EXISTS tickets_jira_id ON tickets(jira_id);
CREATE INDEX IF NOT EXISTS tickets_parent ON tickets(parent);
CREATE INDEX IF NOT EXISTS tickets_type ON tickets(type, project);
CREATE VIRTUAL TABLE IF NOT EXISTS tickets_fts USING fts5(
    key UNINDEXED, summary, description, comments, labels, components,
    tokenize = 'porter unicode61'
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class KbError(Exception):
    pass


@dataclass
class UpsertResult:
    status: str
    old_key: str | None = None
    displaced_key: str | None = None


def terms(text: str) -> list[str]:
    return list(dict.fromkeys(t.lower() for t in re.findall(r"\w{2,}", text)))


def fts_query(text: str, mode: str) -> str | None:
    parts = [f'"{t}"' for t in terms(text)]
    if not parts:
        return None
    return (" AND " if mode == "all" else " OR ").join(parts)


class KbStore:
    def __init__(self, kb_path: str | Path):
        self.root = Path(kb_path).expanduser()
        private_dir(self.root)
        self.db = sqlite3.connect(self.root / DB_NAME)
        (self.root / DB_NAME).chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self._migrate()
        self.db.executescript(_SCHEMA)
        self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _migrate(self) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        exists = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tickets'"
        ).fetchone()
        if not exists or version >= 2:
            return
        columns = {r["name"] for r in self.db.execute("PRAGMA table_info(tickets)")}
        if "jira_id" not in columns:
            self.db.execute("ALTER TABLE tickets ADD COLUMN jira_id TEXT")
        # Tickets from before schema v2 carry no Jira id. Dropping the sync cursors makes the
        # next sync a full one, which adopts each row by key and fills in its id.
        self.db.execute("DELETE FROM meta WHERE key LIKE 'cursor:%'")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.db.commit()

    def bind_site(self, site_url: str) -> None:
        bound = self.get_meta("site_url")
        if bound and bound != site_url:
            raise KbError(
                f"this knowledge base belongs to {bound}, not {site_url}; "
                "choose a different kb_path for the new site"
            )
        if not bound:
            self.set_meta("site_url", site_url)

    def _row(self, where: str, arg) -> sqlite3.Row | None:
        return self.db.execute(
            f"SELECT id, jira_id, key, data FROM tickets WHERE {where}", (arg,)
        ).fetchone()

    def _remove_row(self, rowid: int) -> None:
        self.db.execute("DELETE FROM tickets_fts WHERE rowid = ?", (rowid,))
        self.db.execute("DELETE FROM tickets WHERE id = ?", (rowid,))

    def upsert(self, t: dict) -> UpsertResult:
        jira_id = str(t["id"]) if t.get("id") else None
        by_id = self._row("jira_id = ?", jira_id) if jira_id else None
        by_key = self._row("key = ?", t["key"])
        target = by_id
        if target is None and by_key is not None and (by_key["jira_id"] is None or not jira_id):
            target = by_key
        displaced = None
        if by_key is not None and (target is None or by_key["id"] != target["id"]):
            displaced = by_key["key"]
            self._remove_row(by_key["id"])

        if (
            target is not None
            and json.loads(target["data"]) == t
            and (target["jira_id"] == jira_id or jira_id is None)
        ):
            return UpsertResult("unchanged", displaced_key=displaced)

        comments = "\n".join(c.get("body", "") for c in t.get("comments", []))
        cols = (
            t["key"],
            t["project"],
            t.get("summary"),
            t.get("type"),
            t.get("status"),
            t.get("priority"),
            t.get("assignee"),
            t.get("reporter"),
            t.get("parent"),
            json.dumps(t.get("labels", [])),
            json.dumps(t.get("components", [])),
            t.get("description"),
            t.get("created"),
            t.get("updated"),
            json.dumps(t),
        )
        if target is not None:
            rowid = target["id"]
            self.db.execute(
                "UPDATE tickets SET key=?, project=?, summary=?, type=?, status=?, priority=?, "
                "assignee=?, reporter=?, parent=?, labels=?, components=?, description=?, "
                "created=?, updated=?, data=?, jira_id=COALESCE(?, jira_id) WHERE id=?",
                (*cols, jira_id, rowid),
            )
            self.db.execute("DELETE FROM tickets_fts WHERE rowid = ?", (rowid,))
            status = "updated"
        else:
            rowid = self.db.execute(
                "INSERT INTO tickets(key, project, summary, type, status, priority, assignee, "
                "reporter, parent, labels, components, description, created, updated, data, "
                "jira_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*cols, jira_id),
            ).lastrowid
            status = "new"
        self.db.execute(
            "INSERT INTO tickets_fts"
            "(rowid, key, summary, description, comments, labels, components) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                rowid,
                t["key"],
                t.get("summary") or "",
                t.get("description") or "",
                comments,
                " ".join(t.get("labels", [])),
                " ".join(t.get("components", [])),
            ),
        )
        old_key = target["key"] if target is not None and target["key"] != t["key"] else None
        return UpsertResult(status, old_key=old_key, displaced_key=displaced)

    def commit(self) -> None:
        self.db.commit()

    def delete(self, key: str) -> None:
        self.remove(key=key)

    def remove(self, *, jira_id: str | None = None, key: str | None = None) -> str | None:
        """Delete one ticket by Jira id (preferred) or key; returns the key that was removed."""
        row = self._row("jira_id = ?", jira_id) if jira_id else self._row("key = ?", key)
        if row is None:
            return None
        self._remove_row(row["id"])
        return row["key"]

    def rows(self, project: str) -> list[dict]:
        found = self.db.execute(
            "SELECT jira_id, key, updated FROM tickets WHERE project = ? ORDER BY key", (project,)
        )
        return [dict(r) for r in found]

    def legacy_count(self, project: str | None = None) -> int:
        sql, args = "SELECT COUNT(*) FROM tickets WHERE jira_id IS NULL", ()
        if project:
            sql, args = sql + " AND project = ?", (project,)
        return self.db.execute(sql, args).fetchone()[0]

    def get(self, key: str) -> dict | None:
        row = self.db.execute("SELECT data FROM tickets WHERE key = ?", (key,)).fetchone()
        return json.loads(row["data"]) if row else None

    def keys(self, project: str) -> set[str]:
        rows = self.db.execute("SELECT key FROM tickets WHERE project = ?", (project,))
        return {r["key"] for r in rows}

    def count(self, project: str | None = None) -> int:
        if project:
            sql, args = "SELECT COUNT(*) FROM tickets WHERE project = ?", (project,)
        else:
            sql, args = "SELECT COUNT(*) FROM tickets", ()
        return self.db.execute(sql, args).fetchone()[0]

    def all_tickets(self, project: str) -> list[dict]:
        rows = self.db.execute("SELECT data FROM tickets WHERE project = ?", (project,))
        return [json.loads(r["data"]) for r in rows]

    def children(self, parent_key: str) -> list[dict]:
        found = self.db.execute("SELECT data FROM tickets WHERE parent = ?", (parent_key,))
        return [json.loads(r["data"]) for r in found]

    def recent_of_type(self, type_: str, project: str, limit: int = 100) -> list[dict]:
        found = self.db.execute(
            "SELECT data FROM tickets WHERE type = ? AND project = ? ORDER BY updated DESC LIMIT ?",
            (type_, project, limit),
        )
        return [json.loads(r["data"]) for r in found]

    def epics(self, project: str | None = None) -> list[dict]:
        sql, args = "SELECT data FROM tickets WHERE type = 'Epic'", []
        if project:
            sql, args = sql + " AND project = ?", [project]
        return [json.loads(r["data"]) for r in self.db.execute(sql, args)]

    def search(
        self,
        text: str,
        limit: int = 10,
        project: str | None = None,
        type_: str | None = None,
        mode: str = "auto",
    ) -> list[dict]:
        """mode "auto": all words first, any word if that finds nothing; "any": any word."""
        for how in ("any",) if mode == "any" else ("all", "any"):
            query = fts_query(text, how)
            if not query:
                return []
            hits = self._match(query, limit, project, type_)
            if hits:
                return hits
        return []

    def _match(
        self, query: str, limit: int, project: str | None, type_: str | None = None
    ) -> list[dict]:
        sql = (
            "SELECT t.key, t.summary, t.status, t.type, t.project, t.updated, "
            "bm25(tickets_fts, 0.0, 8.0, 3.0, 1.0, 2.0, 2.0) AS score, "
            "snippet(tickets_fts, 2, '[', ']', '...', 16) AS snippet "
            "FROM tickets_fts JOIN tickets t ON t.id = tickets_fts.rowid "
            "WHERE tickets_fts MATCH ?"
        )
        args: list = [query]
        if project:
            sql += " AND t.project = ?"
            args.append(project)
        if type_:
            sql += " AND t.type = ?"
            args.append(type_)
        sql += " ORDER BY score LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.db.execute(sql, args)]
