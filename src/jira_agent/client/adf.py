import re

_BULLET = re.compile(r"^\s*[-*]\s+(.*)")
_ORDERED = re.compile(r"^\s*\d+[.)]\s+(.*)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)")


def _text(value: str) -> list[dict]:
    return [{"type": "text", "text": value}] if value else []


def _paragraph(lines: list[str]) -> dict:
    content: list[dict] = []
    for i, line in enumerate(lines):
        if i:
            content.append({"type": "hardBreak"})
        content += _text(line)
    return {"type": "paragraph", "content": content}


def _list(kind: str, items: list[str]) -> dict:
    return {
        "type": kind,
        "content": [
            {"type": "listItem", "content": [{"type": "paragraph", "content": _text(i)}]}
            for i in items
        ],
    }


def to_adf(text: str) -> dict:
    blocks: list[dict] = []
    para: list[str] = []
    items: list[str] = []
    list_kind: str | None = None

    def flush_para():
        nonlocal para
        if para:
            blocks.append(_paragraph(para))
            para = []

    def flush_list():
        nonlocal items, list_kind
        if items and list_kind:
            blocks.append(_list(list_kind, items))
        items, list_kind = [], None

    for line in text.splitlines():
        bullet, ordered, heading = _BULLET.match(line), _ORDERED.match(line), _HEADING.match(line)
        if not line.strip():
            flush_para()
            flush_list()
        elif bullet or ordered:
            flush_para()
            kind = "bulletList" if bullet else "orderedList"
            if list_kind and list_kind != kind:
                flush_list()
            list_kind = kind
            items.append((bullet or ordered).group(1))
        elif heading:
            flush_para()
            flush_list()
            blocks.append(
                {
                    "type": "heading",
                    "attrs": {"level": len(heading.group(1))},
                    "content": _text(heading.group(2)),
                }
            )
        else:
            flush_list()
            para.append(line)
    flush_para()
    flush_list()
    return {"version": 1, "type": "doc", "content": blocks}


def from_adf(node) -> str:
    if not node:
        return ""
    if isinstance(node, str):
        return node
    return _render(node).strip()


def _render(node: dict, prefix: str = "") -> str:
    kind = node.get("type")
    children = node.get("content") or []
    if kind == "text":
        return node.get("text", "")
    if kind == "hardBreak":
        return "\n"
    if kind == "mention":
        return node.get("attrs", {}).get("text", "@user")
    if kind in ("bulletList", "orderedList"):
        lines = []
        for n, item in enumerate(children, 1):
            marker = "-" if kind == "bulletList" else f"{n}."
            body = "".join(_render(c) for c in item.get("content") or []).strip()
            lines.append(f"{marker} {body}")
        return "\n".join(lines) + "\n\n"
    inner = "".join(_render(c) for c in children)
    if kind == "heading":
        return f"{'#' * node.get('attrs', {}).get('level', 1)} {inner}\n\n"
    if kind in ("paragraph", "codeBlock", "blockquote"):
        return inner + "\n\n"
    return inner
