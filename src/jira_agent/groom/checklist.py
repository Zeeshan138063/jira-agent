import re

PASS, PARTIAL, FAIL, SKIPPED = "pass", "partial", "fail", "skipped"
_VALUE = {PASS: 1.0, PARTIAL: 0.5, FAIL: 0.0}
GENERIC_TITLES = {"fix", "update", "bug", "issue", "change", "task", "test", "todo", "misc"}
CONVENTION_MIN_PCT = 50
HABIT_MIN_SAMPLE = 10
MIN_BULLETS = 3

_AC = re.compile(
    r"acceptance criteria|\bgiven\b[\s\S]*\bwhen\b[\s\S]*\bthen\b|^\s*[-*]\s*\[[ xX]\]",
    re.I | re.M,
)
_EXPECTATION = re.compile(r"\b(should|must|expected|needs? to)\b", re.I)
_SCOPE = re.compile(r"out of scope|non-?goals?|in scope|\bscope\b", re.I)
_DEPENDENCY = re.compile(r"depends on|blocked by|blocks|dependenc|prerequisite", re.I)
_REPRO = re.compile(r"steps to reproduce|reproduc", re.I)
_EXPECTED = re.compile(r"expected", re.I)
_ACTUAL = re.compile(r"actual", re.I)
_GOAL = re.compile(
    r"\bgoals?\b|objective|\bpurpose\b|\bso that\b|in order to"
    r"|\bas (?:an?|the)\b[^.\n]{0,80}\bi (?:want|need)\b",
    re.I,
)
_SUCCESS = re.compile(
    r"success|\bmetrics?\b|\bkpis?\b|\bmeasures?\b|\btargets?\b|outcome"
    r"|definition of done|done when",
    re.I,
)
_REQUIREMENTS = re.compile(r"^\s*#*\s*\**requirements?\**\s*:?\s*$", re.I | re.M)
_BULLET = re.compile(r"^\s*[-*]\s+\S", re.M)


def has_acceptance(desc: str) -> bool:
    return bool(_AC.search(desc))


def has_scope(desc: str) -> bool:
    return bool(_SCOPE.search(desc))


def has_requirements_list(desc: str) -> bool:
    return bool(_REQUIREMENTS.search(desc)) and len(_BULLET.findall(desc)) >= MIN_BULLETS


def repro_parts(desc: str) -> int:
    return sum(1 for rx in (_REPRO, _EXPECTED, _ACTUAL) if rx.search(desc))


def has_goal(desc: str) -> bool:
    return bool(_GOAL.search(desc))


def has_outcomes(desc: str) -> bool:
    """A way to tell it is done: success wording, acceptance criteria or a requirements list."""
    return bool(_SUCCESS.search(desc)) or has_acceptance(desc) or has_requirements_list(desc)


def habit_pct(conv: dict, issue_type: str | None, name: str) -> int | None:
    """How often tickets of this type in the project do `name`; None when it is not known."""
    habits = (conv or {}).get("habits") or {}
    own = habits.get(issue_type or "")
    if own and own.get("n", 0) >= HABIT_MIN_SAMPLE:
        return own.get(name)
    if issue_type == "Epic":
        return None
    fallback = habits.get("_non_epic")
    if fallback and fallback.get("n", 0) >= HABIT_MIN_SAMPLE:
        return fallback.get(name)
    return None


def habit_weight(base: int, pct: int | None) -> int:
    """Scale a check's weight by how much the project actually does it (0 = not a habit)."""
    if pct is None or pct >= 50:
        return base
    if pct >= 20:
        return max(1, round(base * 2 / 3))
    if pct >= 10:
        return 1
    return 0


def _item(id_, label, weight, status, detail, suggestion=None):
    return {
        "id": id_,
        "label": label,
        "weight": weight,
        "status": status,
        "detail": detail,
        "suggestion": suggestion if status in (PARTIAL, FAIL) else None,
    }


def _habit_note(pct: int | None, base: int, weight: int, kind: str | None) -> str:
    if pct is None or weight >= base:
        return ""
    return f"; only {pct}% of {kind or 'these'} tickets here do this, so it weighs less"


def _title(t):
    summary = (t.get("summary") or "").strip()
    words = len(summary.split())
    if words < 3 or summary.lower().strip(" .") in GENERIC_TITLES:
        return _item(
            "title",
            "Clear title",
            2,
            FAIL,
            f"{words} word(s)",
            "Name the outcome or the broken behaviour in 5-12 words.",
        )
    if words < 5 or len(summary) > 120:
        return _item(
            "title",
            "Clear title",
            2,
            PARTIAL,
            f"{words} words, {len(summary)} chars",
            "Aim for 5-12 words that say what changes and where.",
        )
    return _item("title", "Clear title", 2, PASS, f"{words} words")


def _problem(t):
    size = len((t.get("description") or "").strip())
    if size >= 150:
        return _item("problem", "Problem and context", 3, PASS, f"{size} chars")
    if size >= 40:
        return _item(
            "problem",
            "Problem and context",
            3,
            PARTIAL,
            f"{size} chars",
            "Explain the problem, who is affected and why it matters now.",
        )
    return _item(
        "problem",
        "Problem and context",
        3,
        FAIL,
        f"{size} chars",
        "Add a description: problem, who is affected, why now.",
    )


def _criteria(t, conv):
    desc = t.get("description") or ""
    kind = t.get("type")
    pct = habit_pct(conv, kind, "acceptance")
    weight = habit_weight(3, pct)
    note = _habit_note(pct, 3, weight, kind)
    label = "Acceptance criteria"
    if has_acceptance(desc):
        return _item("acceptance", label, max(weight, 1), PASS, "found")
    listed = has_requirements_list(desc)
    if listed and pct is not None and pct < 20:
        return _item(
            "acceptance", label, max(weight, 1), PASS, "requirements list (this project's norm)"
        )
    if weight == 0:
        return _item("acceptance", label, 1, SKIPPED, f"not a habit for {kind} here ({pct}%)")
    if listed or _EXPECTATION.search(desc):
        return _item(
            "acceptance",
            label,
            weight,
            PARTIAL,
            "implied, not explicit" + note,
            "Turn the expectations into testable Given/When/Then or checklist items.",
        )
    return _item(
        "acceptance",
        label,
        weight,
        FAIL,
        "none" + note,
        "Add 2-5 testable criteria so reviewers know when it is done.",
    )


def _scope(t, conv):
    kind = t.get("type")
    if kind == "Bug":
        return _item("scope", "Scope boundaries", 1, SKIPPED, "not required for bugs")
    pct = habit_pct(conv, kind, "scope")
    weight = habit_weight(1, pct)
    if has_scope(t.get("description") or ""):
        return _item("scope", "Scope boundaries", 1, PASS, "found")
    if weight == 0:
        return _item(
            "scope", "Scope boundaries", 1, SKIPPED, f"not a habit for {kind} here ({pct}%)"
        )
    return _item(
        "scope",
        "Scope boundaries",
        weight,
        FAIL,
        "none",
        "State what is in scope and what is explicitly out.",
    )


def _dependencies(t):
    if t.get("links") or _DEPENDENCY.search(t.get("description") or ""):
        return _item("dependencies", "Dependencies", 1, PASS, "links or mentions found")
    return _item(
        "dependencies",
        "Dependencies",
        1,
        PARTIAL,
        "none recorded",
        "Confirm there are no blockers, or link the tickets this depends on.",
    )


def _estimate(t):
    if "estimate" not in t:
        return _item("estimate", "Estimate", 1, SKIPPED, "not available")
    if t["estimate"]:
        return _item("estimate", "Estimate", 1, PASS, str(t["estimate"]))
    return _item("estimate", "Estimate", 1, FAIL, "missing", "Add an estimate before planning.")


def _classification(t, conv):
    wants_labels = conv.get("with_labels_pct", 0) >= CONVENTION_MIN_PCT
    wants_components = conv.get("with_components_pct", 0) >= CONVENTION_MIN_PCT
    if not (wants_labels or wants_components):
        return _item("classification", "Labels / components", 1, SKIPPED, "not a project habit")
    ok = (wants_labels and t.get("labels")) or (wants_components and t.get("components"))
    if ok:
        return _item("classification", "Labels / components", 1, PASS, "present")
    need = " or ".join(
        n for n, w in (("labels", wants_labels), ("a component", wants_components)) if w
    )
    return _item(
        "classification",
        "Labels / components",
        1,
        FAIL,
        "missing",
        f"This project usually sets {need}; add what fits.",
    )


def _parent(t, conv):
    if t.get("type") == "Epic" or conv.get("with_parent_pct", 0) < CONVENTION_MIN_PCT:
        return _item("parent", "Parent epic", 1, SKIPPED, "not a project habit")
    if t.get("parent"):
        return _item("parent", "Parent epic", 1, PASS, t["parent"])
    return _item(
        "parent",
        "Parent epic",
        1,
        FAIL,
        "missing",
        "Most tickets here sit under an epic or parent; pick one.",
    )


def _priority(t):
    if t.get("priority"):
        return _item("priority", "Priority", 1, PASS, t["priority"])
    return _item("priority", "Priority", 1, FAIL, "unset", "Set a priority.")


def _repro(t, conv):
    if t.get("type") != "Bug":
        return _item("repro", "Reproduction details", 3, SKIPPED, "bugs only")
    pct = habit_pct(conv, "Bug", "repro")
    # A bug nobody can reproduce is a problem whatever the project's habits, so this check
    # keeps a minimum weight instead of dropping out.
    weight = max(habit_weight(3, pct), 1)
    found = repro_parts(t.get("description") or "")
    if found == 3:
        return _item("repro", "Reproduction details", weight, PASS, "steps, expected and actual")
    return _item(
        "repro",
        "Reproduction details",
        weight,
        PARTIAL if found else FAIL,
        f"{found}/3 parts" + _habit_note(pct, 3, weight, "Bug"),
        "Add steps to reproduce, expected result and actual result.",
    )


def _goal(t, conv):
    desc = (t.get("description") or "").strip()
    pct = habit_pct(conv, "Epic", "goal")
    weight = max(habit_weight(3, pct), 1)
    note = _habit_note(pct, 3, weight, "Epic")
    if has_goal(desc) and len(desc) >= 100:
        return _item("goal", "Goal and why it matters", weight, PASS, "stated")
    if has_goal(desc) or len(desc) >= 60:
        return _item(
            "goal",
            "Goal and why it matters",
            weight,
            PARTIAL,
            ("goal wording but thin" if has_goal(desc) else "described, goal not explicit") + note,
            "Say who benefits, what changes for them and why it matters now "
            '("As a ... I want ... so that ...").',
        )
    return _item(
        "goal",
        "Goal and why it matters",
        weight,
        FAIL,
        f"{len(desc)} chars" + note,
        "State the goal: who benefits, what changes and why now.",
    )


def _outcomes(t, conv):
    desc = t.get("description") or ""
    pct = habit_pct(conv, "Epic", "outcomes")
    weight = max(habit_weight(3, pct), 1)
    note = _habit_note(pct, 3, weight, "Epic")
    label = "Success measures or requirements"
    if has_outcomes(desc):
        return _item("outcomes", label, weight, PASS, "found")
    if _EXPECTATION.search(desc) or len(_BULLET.findall(desc)) >= 1:
        return _item(
            "outcomes",
            label,
            weight,
            PARTIAL,
            "implied, not explicit" + note,
            "List what the epic must deliver, or how you will know it worked (a metric or target).",
        )
    return _item(
        "outcomes",
        label,
        weight,
        FAIL,
        "none" + note,
        "Add the requirements the epic must deliver, or a success measure.",
    )


def _breakdown(children: int | None):
    if children is None:
        return _item("breakdown", "Broken into child tickets", 1, SKIPPED, "not checked")
    if children:
        return _item("breakdown", "Broken into child tickets", 1, PASS, f"{children} child tickets")
    return _item(
        "breakdown",
        "Broken into child tickets",
        1,
        PARTIAL,
        "no child tickets yet",
        "Break the epic into stories, or confirm it is a placeholder.",
    )


def evaluate(ticket: dict, conventions: dict | None = None, children: int | None = None) -> dict:
    conv = conventions or {}
    if ticket.get("type") == "Epic":
        items = [
            _title(ticket),
            _goal(ticket, conv),
            _outcomes(ticket, conv),
            _scope(ticket, conv),
            _breakdown(children),
            _dependencies(ticket),
            _classification(ticket, conv),
            _parent(ticket, conv),
            _priority(ticket),
        ]
    else:
        items = [
            _title(ticket),
            _problem(ticket),
            _criteria(ticket, conv),
            _repro(ticket, conv),
            _scope(ticket, conv),
            _dependencies(ticket),
            _estimate(ticket),
            _classification(ticket, conv),
            _parent(ticket, conv),
            _priority(ticket),
        ]
    active = [i for i in items if i["status"] != SKIPPED]
    total = sum(i["weight"] for i in active)
    earned = sum(i["weight"] * _VALUE[i["status"]] for i in active)
    score = round(100 * earned / total) if total else 0
    grade = "ready" if score >= 80 else "needs_work" if score >= 50 else "poor"
    return {
        "key": ticket.get("key"),
        "score": score,
        "grade": grade,
        "gaps": [i for i in items if i["status"] in (PARTIAL, FAIL)],
        "items": items,
    }
