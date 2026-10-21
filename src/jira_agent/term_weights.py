"""How much each word says about what a ticket is, learned from the project itself.

In a project built from templates ("Team Overview API", "Billing Overview API") the shared words
say almost nothing and the one word that differs says everything. Weighting words by how rare they
are in the project (inverse document frequency) lets the duplicate check see that, and titles that
repeat across the project are treated as templates, not as evidence of a duplicate.
"""

import json
import math
import re
from collections import Counter

from jira_agent.kb.store import KbStore, terms
from jira_agent.kb.summarize import STOPWORDS

VERSION = 1
BODY_CHARS = 600
TEMPLATE_MIN_COUNT = 3
MIN_TICKETS = 30
_SUFFIXES = ("ing", "ed", "es", "s")


def stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def term_set(text: str | None) -> set[str]:
    return {stem(t) for t in terms(text or "") if t not in STOPWORDS}


def normalize_title(summary: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (summary or "").lower()).strip()


def body_terms(ticket: dict) -> set[str]:
    return term_set(f"{ticket.get('summary')} {(ticket.get('description') or '')[:BODY_CHARS]}")


class TermWeights:
    def __init__(
        self,
        n: int,
        df: dict[str, int],
        titles: dict[str, int],
        use_idf: bool = True,
        use_template: bool = True,
    ):
        self.n = n
        self.df = df
        self.titles = titles
        self.use_idf = use_idf
        self.use_template = use_template
        self._default = math.log((n + 1) / 2) + 1

    def weight(self, term: str) -> float:
        if not self.use_idf:
            return 1.0
        df = self.df.get(term)
        return self._default if df is None else math.log((self.n + 1) / (df + 1)) + 1

    def is_template(self, summary: str | None) -> bool:
        if not self.use_template:
            return False
        title = normalize_title(summary)
        return len(title) > 8 and self.titles.get(title, 0) >= TEMPLATE_MIN_COUNT

    def with_options(self, use_idf: bool, use_template: bool) -> "TermWeights":
        return TermWeights(self.n, self.df, self.titles, use_idf, use_template)

    @classmethod
    def build(cls, tickets: list[dict]) -> "TermWeights":
        df: Counter = Counter()
        titles: Counter = Counter()
        for t in tickets:
            df.update(body_terms(t))
            title = normalize_title(t.get("summary"))
            if title:
                titles[title] += 1
        return cls(
            len(tickets),
            {term: c for term, c in df.items() if c >= 2},
            {title: c for title, c in titles.items() if c >= 2},
        )

    def to_json(self) -> str:
        return json.dumps({"v": VERSION, "n": self.n, "df": self.df, "titles": self.titles})

    @classmethod
    def from_json(cls, raw: str) -> "TermWeights | None":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if data.get("v") != VERSION:
            return None
        return cls(data["n"], data["df"], data["titles"])


def project_of(key: str) -> str:
    return key.rsplit("-", 1)[0]


def get_weights(kb: KbStore, project: str) -> TermWeights:
    """Word weights for a project, cached in the index and rebuilt when the ticket count has
    drifted; small edits between rebuilds barely move them."""
    count = kb.count(project)
    raw = kb.get_meta(f"weights:{project}")
    cached = TermWeights.from_json(raw) if raw else None
    if cached and abs(cached.n - count) <= max(5, count // 50):
        return cached
    built = TermWeights.build(kb.all_tickets(project))
    kb.set_meta(f"weights:{project}", built.to_json())
    kb.commit()
    return built


def weights_for(kb: KbStore, projects: list[str]) -> dict[str, TermWeights]:
    return {p: get_weights(kb, p) for p in projects if kb.count(p) >= MIN_TICKETS}
