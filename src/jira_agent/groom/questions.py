DEFAULT_TYPES = ["Story", "Bug", "Task", "Spike"]
DEFAULT_PRIORITIES = ["High", "Medium", "Low"]
MAX_OPTIONS = 4
ROUNDS = (1, 2, 3)


def _q(key: str, question: str, header: str, options: list[dict], multi: bool = False) -> dict:
    return {
        "id": key,
        "question": question,
        "header": header,
        "multi_select": multi,
        "options": options[:MAX_OPTIONS],
    }


def _opt(label: str, description: str, recommended: bool = False) -> dict:
    return {
        "label": f"{label} (Recommended)" if recommended else label,
        "value": label,
        "description": description,
        "recommended": recommended,
    }


def _names(pairs: list[tuple[str, int]]) -> list[str]:
    return [name for name, _ in pairs if name]


def recent_epics(tickets: list[dict], n: int = 3) -> list[dict]:
    epics = [t for t in tickets if t.get("type") == "Epic" and not t.get("resolution")]
    epics.sort(key=lambda t: t.get("updated") or "", reverse=True)
    return epics[:n]


def build_round(round_no: int, stats: dict, epics: list[dict], default_type: str = "Task") -> dict:
    if round_no not in ROUNDS:
        raise ValueError(f"round must be one of {ROUNDS}")
    if round_no == 1:
        types = [t for t in _names(stats.get("types", [])) if t != "Epic"] or DEFAULT_TYPES
        preferred = default_type if default_type in types else types[0]
        type_opts = [
            _opt(t, f"Create a {t}", recommended=(t == preferred)) for t in types[:MAX_OPTIONS]
        ]
        components = _names(stats.get("components", []))[: MAX_OPTIONS - 1]
        comp_opts = [_opt(c, f"Belongs to the {c} component") for c in components]
        comp_opts.append(_opt("None / not sure", "Leave the component empty"))
        if len(comp_opts) < 2:
            comp_opts.append(_opt("I will name it", "Type the component yourself"))
        epic_opts = [_opt(e["key"], e.get("summary") or "") for e in epics]
        epic_opts.append(_opt("Standalone", "No parent epic", recommended=not epics))
        if len(epic_opts) < 2:
            epic_opts.append(_opt("I will name one", "Type the parent ticket key"))
        return {
            "round": 1,
            "title": "Type and placement",
            "questions": [
                _q("type", "What kind of ticket is this?", "Type", type_opts),
                _q("component", "Which area does it belong to?", "Component", comp_opts),
                _q("parent", "Is it part of larger work?", "Parent", epic_opts),
            ],
        }
    if round_no == 2:
        return {
            "round": 2,
            "title": "Scope and acceptance",
            "questions": [
                _q(
                    "acceptance_style",
                    "How should we describe 'done'?",
                    "Done when",
                    [
                        _opt("Given/When/Then", "Testable scenarios", recommended=True),
                        _opt("Checklist", "Short list of verifiable outcomes"),
                        _opt("Metric or target", "A measurable result to hit"),
                    ],
                ),
                _q(
                    "scope",
                    "Should the ticket spell out what is out of scope?",
                    "Scope",
                    [
                        _opt("Yes, list exclusions", "Add an in/out of scope section", True),
                        _opt("No, keep it short", "Small and self-evident change"),
                    ],
                ),
                _q(
                    "detail",
                    "How much detail do you want me to draft?",
                    "Detail",
                    [
                        _opt("Standard", "Context, criteria, scope", recommended=True),
                        _opt("Brief", "A few lines for a small change"),
                        _opt("Detailed", "Adds approach, risks and test notes"),
                    ],
                ),
            ],
        }
    priorities = _names(stats.get("priorities", [])) or DEFAULT_PRIORITIES
    labels = _names(stats.get("labels", []))[:MAX_OPTIONS]
    label_opts = [_opt(label, f"Add the {label} label") for label in labels]
    label_opts.append(_opt("No labels", "Skip labels"))
    if len(label_opts) < 2:
        label_opts.append(_opt("I will name them", "Type the labels yourself"))
    return {
        "round": 3,
        "title": "Priority and dependencies",
        "questions": [
            _q(
                "priority",
                "What priority?",
                "Priority",
                [_opt(p, f"{p} priority", recommended=(i == 1)) for i, p in enumerate(priorities)],
            ),
            _q(
                "dependencies",
                "Does it depend on, or block, other work?",
                "Dependencies",
                [
                    _opt("None", "No known dependencies", recommended=True),
                    _opt("Blocked by an existing ticket", "I will name the ticket"),
                    _opt("Blocks other work", "I will name what it blocks"),
                ],
            ),
            _q("labels", "Which labels apply?", "Labels", label_opts, multi=True),
        ],
    }
