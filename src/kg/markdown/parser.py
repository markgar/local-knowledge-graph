from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
TASK_RE = re.compile(r"^[ \t]*[-*+][ \t]+\[([ xX])\][ \t]+(.+?)\s*$")
LIST_RE = re.compile(r"^[ \t]*[-*+][ \t]+(.+?)\s*$")
WIKILINK_RE = re.compile(r"\[\[([^]|#]+)(?:#[^]|]+)?(?:\|[^]]+)?]]")


class MarkdownParseError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedTask:
    text: str
    status: str
    owner: str | None = None
    due_date: str | None = None


@dataclass(frozen=True)
class ParsedAnchor:
    structural_path: str
    heading_path: tuple[str, ...]
    kind: str
    start_offset: int
    end_offset: int
    quote: str
    wikilinks: tuple[str, ...] = ()
    task: ParsedTask | None = None


@dataclass(frozen=True)
class ParsedDocument:
    title: str
    frontmatter: dict[str, object]
    anchors: list[ParsedAnchor] = field(default_factory=list)


def parse_markdown(text: str, fallback_title: str) -> ParsedDocument:
    frontmatter, body_start = _parse_frontmatter(text)
    lines = text.splitlines(keepends=True)
    offsets = _line_offsets(lines)
    heading_stack: list[tuple[int, str]] = []
    anchors: list[ParsedAnchor] = []
    paragraph_start: int | None = None
    paragraph_lines: list[str] = []
    title = str(frontmatter.get("title") or fallback_title)
    anchor_index = 0

    def flush_paragraph() -> None:
        nonlocal paragraph_start, paragraph_lines, anchor_index
        if paragraph_start is None:
            return
        quote = "".join(paragraph_lines).strip()
        if quote:
            start = offsets[paragraph_start] + len(
                "".join(paragraph_lines)
            ) - len("".join(paragraph_lines).lstrip())
            end = start + len(quote)
            anchor_index += 1
            anchors.append(
                _anchor(
                    anchor_index,
                    heading_stack,
                    "paragraph",
                    start,
                    end,
                    quote,
                )
            )
        paragraph_start = None
        paragraph_lines = []

    for line_index, line in enumerate(lines):
        if offsets[line_index] < body_start:
            continue
        content = line.rstrip("\r\n")
        heading = HEADING_RE.match(content)
        task = TASK_RE.match(content)
        list_item = LIST_RE.match(content)

        if heading:
            flush_paragraph()
            level, value = len(heading.group(1)), heading.group(2).strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, value))
            if level == 1 and title == fallback_title:
                title = value
            anchor_index += 1
            start = offsets[line_index]
            anchors.append(
                _anchor(anchor_index, heading_stack, "heading", start, start + len(content), content)
            )
        elif task:
            flush_paragraph()
            anchor_index += 1
            start = offsets[line_index]
            task_text = task.group(2).strip()
            anchors.append(
                _anchor(
                    anchor_index,
                    heading_stack,
                    "task",
                    start,
                    start + len(content),
                    content,
                    ParsedTask(
                        text=task_text,
                        status="completed" if task.group(1).casefold() == "x" else "open",
                    ),
                )
            )
        elif list_item:
            flush_paragraph()
            anchor_index += 1
            start = offsets[line_index]
            anchors.append(
                _anchor(
                    anchor_index,
                    heading_stack,
                    "list_item",
                    start,
                    start + len(content),
                    content,
                )
            )
        elif content.strip():
            if paragraph_start is None:
                paragraph_start = line_index
            paragraph_lines.append(line)
        else:
            flush_paragraph()

    flush_paragraph()
    return ParsedDocument(title=title, frontmatter=frontmatter, anchors=anchors)


def _parse_frontmatter(text: str) -> tuple[dict[str, object], int]:
    if not text.startswith("---"):
        return {}, 0
    first_line_end = text.find("\n")
    if first_line_end == -1 or text[:first_line_end].strip() != "---":
        return {}, 0
    closing = re.search(r"(?m)^---[ \t]*\r?$", text[first_line_end + 1 :])
    if not closing:
        return {}, 0
    yaml_start = first_line_end + 1
    yaml_end = yaml_start + closing.start()
    body_start = yaml_start + closing.end()
    if body_start < len(text) and text[body_start] == "\n":
        body_start += 1
    try:
        parsed = yaml.safe_load(text[yaml_start:yaml_end]) or {}
    except yaml.YAMLError as exc:
        raise MarkdownParseError(f"Invalid YAML frontmatter: {exc}") from exc
    return parsed if isinstance(parsed, dict) else {}, body_start


def _line_offsets(lines: list[str]) -> list[int]:
    offsets: list[int] = []
    current = 0
    for line in lines:
        offsets.append(current)
        current += len(line)
    return offsets


def _anchor(
    index: int,
    heading_stack: list[tuple[int, str]],
    kind: str,
    start: int,
    end: int,
    quote: str,
    task: ParsedTask | None = None,
) -> ParsedAnchor:
    headings = tuple(value for _, value in heading_stack)
    path = "/".join([*(f"h{level}:{value}" for level, value in heading_stack), f"{kind}:{index}"])
    links = tuple(match.group(1).strip() for match in WIKILINK_RE.finditer(quote))
    return ParsedAnchor(
        structural_path=path,
        heading_path=headings,
        kind=kind,
        start_offset=start,
        end_offset=end,
        quote=quote,
        wikilinks=links,
        task=task,
    )
