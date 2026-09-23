from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml
from markdown_it import MarkdownIt
from markdown_it.token import Token

TASK_RE = re.compile(r"^[ \t]*(?:[-*+]|[0-9]{1,9}[.)])[ \t]+\[([ xX])\][ \t]+(.+?)\s*$")
WIKILINK_RE = re.compile(r"\[\[([^]|#]+)(?:#[^]|]+)?(?:\|[^]]+)?]]")
INLINE_FIELD_RE = re.compile(r"\[([A-Za-z][A-Za-z0-9_-]*)::\s*([^]]+)]")
RECORD_HEADINGS = {
    "decision": {"decision", "decisions"},
    "blocker": {"blocker", "blockers"},
    "conflict": {"conflict", "conflicts"},
}
MARKDOWN = MarkdownIt("commonmark")
LINE_END_RE = re.compile(r"\r\n?|\n")


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
    offsets = [body_start + offset for offset in _line_offsets(text[body_start:])]
    tokens = MARKDOWN.parse(text[body_start:])
    code_ranges = [
        (offsets[token.map[0]], offsets[token.map[1]])
        for token in tokens
        if token.type in {"fence", "code_block"} and token.map is not None
    ]
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
                offsets,
                token.map[0],
                token.map[1],
            )
            first_line = LINE_END_RE.split(quote, maxsplit=1)[0]
            task_match = TASK_RE.match(first_line)
            semantic_quote = _semantic_quote(_mask_ranges(quote, start, code_ranges))
            child_ranges = []
            for child_index in range(token_index + 1, len(tokens)):
                child = tokens[child_index]
                if child.type == "list_item_close" and child.level == token.level:
                    break
                if child.type == "list_item_open" and child.map is not None:
                    child_ranges.append((offsets[child.map[0]], offsets[child.map[1]]))
            metadata = _inline_fields(_mask_ranges(semantic_quote, start, child_ranges))
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
                    semantic_quote=semantic_quote,
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
                offsets,
                token.map[0],
                token.map[1],
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
                offsets,
                token.map[0],
                token.map[1],
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
                    metadata=_inline_fields(_semantic_quote(quote)),
                    record_type=_record_type(heading_stack),
                )
            )

    return ParsedDocument(title=title, frontmatter=frontmatter, anchors=anchors)


def _parse_frontmatter(text: str) -> tuple[dict[str, object], int]:
    if not text.startswith("---"):
        return {}, 0
    offsets = _line_offsets(text)
    if len(offsets) < 2 or text[: offsets[1]].strip() != "---":
        return {}, 0
    closing_line = next(
        (
            index
            for index in range(1, len(offsets) - 1)
            if re.fullmatch(
                r"---[ \t]*", text[offsets[index] : offsets[index + 1]].rstrip("\r\n")
            )
        ),
        None,
    )
    if closing_line is None:
        return {}, 0
    yaml_start = offsets[1]
    yaml_end = offsets[closing_line]
    body_start = offsets[closing_line + 1]
    try:
        parsed = yaml.safe_load(text[yaml_start:yaml_end]) or {}
    except yaml.YAMLError as exc:
        raise MarkdownParseError(f"Invalid YAML frontmatter: {exc}") from exc
    return parsed if isinstance(parsed, dict) else {}, body_start


def _line_offsets(text: str) -> list[int]:
    # CommonMark only normalizes CR, LF, and CRLF, not Unicode line separators.
    offsets = [0, *(match.end() for match in LINE_END_RE.finditer(text))]
    if offsets[-1] != len(text):
        offsets.append(len(text))
    return offsets


def _source_span(
    text: str,
    offsets: list[int],
    start_line: int,
    end_line: int,
) -> tuple[int, int, str]:
    start = offsets[start_line]
    raw_end = offsets[end_line] if end_line < len(offsets) else len(text)
    raw = text[start:raw_end]
    quote = raw.rstrip()
    return start, start + len(quote), quote


def _mask_ranges(quote: str, start: int, ranges: list[tuple[int, int]]) -> str:
    masked = list(quote)
    for range_start, range_end in ranges:
        left = max(0, range_start - start)
        right = min(len(quote), range_end - start)
        if left < right:
            masked[left:right] = " " * (right - left)
    return "".join(masked)


def _inline_content(tokens: list[Token], index: int) -> str:
    if index + 1 < len(tokens) and tokens[index + 1].type == "inline":
        return tokens[index + 1].content
    return ""


def _inline_fields(quote: str) -> dict[str, str]:
    if re.search(r"\[(?:key|supersedes)::\s*]", quote, re.IGNORECASE):
        raise MarkdownParseError("Record key and supersedes fields must not be empty")
    fields: dict[str, str] = {}
    for match in INLINE_FIELD_RE.finditer(quote):
        name = match.group(1).casefold()
        if name in {"key", "supersedes"} and name in fields:
            raise MarkdownParseError(f"Duplicate {name} field on one record")
        fields[name] = match.group(2).strip()
    return fields


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
    semantic_quote: str | None = None,
    metadata: dict[str, str] | None = None,
    task: ParsedTask | None = None,
    record_type: str | None = None,
) -> ParsedAnchor:
    headings = tuple(value for _, value in heading_stack)
    path = "/".join([*(f"h{level}:{value}" for level, value in heading_stack), f"{kind}:{index}"])
    if semantic_quote is None:
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
