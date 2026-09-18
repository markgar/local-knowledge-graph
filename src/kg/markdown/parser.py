from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml
from markdown_it import MarkdownIt
from markdown_it.token import Token

TASK_RE = re.compile(r"^[ \t]*[-*+][ \t]+\[([ xX])\][ \t]+(.+?)\s*$")
WIKILINK_RE = re.compile(r"\[\[([^]|#]+)(?:#[^]|]+)?(?:\|[^]]+)?]]")
INLINE_FIELD_RE = re.compile(r"\[([A-Za-z][A-Za-z0-9_-]*)::\s*([^]]+)]")
RECORD_HEADINGS = {
    "decision": {"decision", "decisions"},
    "blocker": {"blocker", "blockers"},
    "conflict": {"conflict", "conflicts"},
}
MARKDOWN = MarkdownIt("commonmark")


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
    semantic_quote: str
    wikilinks: tuple[str, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)
    task: ParsedTask | None = None
    record_type: str | None = None


@dataclass(frozen=True)
class ParsedDocument:
    title: str
    frontmatter: dict[str, object]
    anchors: list[ParsedAnchor] = field(default_factory=list)


def parse_markdown(text: str, fallback_title: str) -> ParsedDocument:
    frontmatter, body_start = _parse_frontmatter(text)
    lines = text.splitlines(keepends=True)
    offsets = _line_offsets(lines)
    body_line = text[:body_start].count("\n")
    tokens = MARKDOWN.parse(text[body_start:])
    heading_stack: list[tuple[int, str]] = []
    anchors: list[ParsedAnchor] = []
    title = str(frontmatter.get("title") or fallback_title)
    anchor_index = 0
    list_depth = 0

    for token_index, token in enumerate(tokens):
        if token.type == "list_item_open":
            list_depth += 1
            if token.map is None:
                continue
            start, end, quote = _source_span(
                text,
                lines,
                offsets,
                body_line + token.map[0],
                body_line + token.map[1],
            )
            first_line = quote.splitlines()[0]
            task_match = TASK_RE.match(first_line)
            metadata = _inline_fields(quote)
            task = None
            kind = "list_item"
            if task_match:
                kind = "task"
                task_text = INLINE_FIELD_RE.sub("", task_match.group(2)).strip()
                task = ParsedTask(
                    text=task_text,
                    status="completed" if task_match.group(1).casefold() == "x" else "open",
                    owner=metadata.get("owner"),
                    due_date=metadata.get("due"),
                )
            anchor_index += 1
            anchors.append(
                _anchor(
                    anchor_index,
                    heading_stack,
                    kind,
                    start,
                    end,
                    quote,
                    metadata=metadata,
                    task=task,
                    record_type=_record_type(heading_stack),
                )
            )
        elif token.type == "list_item_close":
            list_depth -= 1
        elif token.type == "heading_open" and token.map is not None:
            level = int(token.tag[1])
            value = _inline_content(tokens, token_index).strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, value))
            if level == 1 and title == fallback_title:
                title = value
            anchor_index += 1
            start, end, quote = _source_span(
                text,
                lines,
                offsets,
                body_line + token.map[0],
                body_line + token.map[1],
            )
            anchors.append(
                _anchor(
                    anchor_index,
                    heading_stack,
                    "heading",
                    start,
                    end,
                    quote,
                )
            )
        elif token.type == "paragraph_open" and token.map is not None and list_depth == 0:
            start, end, quote = _source_span(
                text,
                lines,
                offsets,
                body_line + token.map[0],
                body_line + token.map[1],
            )
            anchor_index += 1
            anchors.append(
                _anchor(
                    anchor_index,
                    heading_stack,
                    "paragraph",
                    start,
                    end,
                    quote,
                    metadata=_inline_fields(quote),
                    record_type=_record_type(heading_stack),
                )
            )

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


def _source_span(
    text: str,
    lines: list[str],
    offsets: list[int],
    start_line: int,
    end_line: int,
) -> tuple[int, int, str]:
    start = offsets[start_line]
    raw_end = offsets[end_line] if end_line < len(offsets) else len(text)
    raw = text[start:raw_end]
    quote = raw.rstrip()
    return start, start + len(quote), quote


def _inline_content(tokens: list[Token], index: int) -> str:
    if index + 1 < len(tokens) and tokens[index + 1].type == "inline":
        return tokens[index + 1].content
    return ""


def _inline_fields(quote: str) -> dict[str, str]:
    return {
        match.group(1).casefold(): match.group(2).strip()
        for match in INLINE_FIELD_RE.finditer(quote)
    }


def _record_type(heading_stack: list[tuple[int, str]]) -> str | None:
    if not heading_stack:
        return None
    heading = heading_stack[-1][1].strip().casefold()
    for record_type, names in RECORD_HEADINGS.items():
        if heading in names:
            return record_type
    return None


def _anchor(
    index: int,
    heading_stack: list[tuple[int, str]],
    kind: str,
    start: int,
    end: int,
    quote: str,
    *,
    metadata: dict[str, str] | None = None,
    task: ParsedTask | None = None,
    record_type: str | None = None,
) -> ParsedAnchor:
    headings = tuple(value for _, value in heading_stack)
    path = "/".join([*(f"h{level}:{value}" for level, value in heading_stack), f"{kind}:{index}"])
    semantic_quote = _semantic_quote(quote)
    links = tuple(
        match.group(1).strip() for match in WIKILINK_RE.finditer(semantic_quote)
    )
    return ParsedAnchor(
        structural_path=path,
        heading_path=headings,
        kind=kind,
        start_offset=start,
        end_offset=end,
        quote=quote,
        semantic_quote=semantic_quote,
        wikilinks=links,
        metadata=metadata or {},
        task=task,
        record_type=record_type,
    )


def _semantic_quote(quote: str) -> str:
    masked = list(quote)
    inline_tokens = MARKDOWN.parseInline(quote)
    children = inline_tokens[0].children if inline_tokens else None
    if children and any(child.type == "code_inline" for child in children):
        for match in re.finditer(r"(`+)(.+?)\1", quote, re.DOTALL):
            masked[match.start() : match.end()] = " " * (match.end() - match.start())
    for match in re.finditer(r"\[\[[^\n]*?]]", quote):
        slash_count = 0
        cursor = match.start() - 1
        while cursor >= 0 and quote[cursor] == "\\":
            slash_count += 1
            cursor -= 1
        if slash_count % 2 == 1:
            start = match.start() - 1
            masked[start : match.end()] = " " * (match.end() - start)
    return "".join(masked)
