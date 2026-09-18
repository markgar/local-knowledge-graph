import pytest

from kg.markdown import MarkdownParseError, parse_markdown


def test_parser_preserves_structure_and_offsets() -> None:
    text = """---
title: Example
---

# Heading

Paragraph with [[Target]].

- [ ] Do the work. [owner:: Avery] [due:: 2026-10-01]
"""

    parsed = parse_markdown(text, "fallback")

    assert parsed.title == "Example"
    assert [anchor.kind for anchor in parsed.anchors] == ["heading", "paragraph", "task"]
    paragraph = parsed.anchors[1]
    assert text[paragraph.start_offset : paragraph.end_offset] == paragraph.quote
    assert paragraph.heading_path == ("Heading",)
    assert paragraph.wikilinks == ("Target",)
    assert parsed.anchors[2].task is not None
    assert parsed.anchors[2].task.status == "open"
    assert parsed.anchors[2].task.text == "Do the work."
    assert parsed.anchors[2].task.owner == "Avery"
    assert parsed.anchors[2].task.due_date == "2026-10-01"


def test_invalid_frontmatter_has_a_stable_parse_error() -> None:
    with pytest.raises(MarkdownParseError, match="Invalid YAML frontmatter"):
        parse_markdown("---\ntitle: [invalid\n---\n\nBody\n", "fallback")


def test_commonmark_blocks_ignore_fenced_heading_syntax() -> None:
    parsed = parse_markdown(
        "# Real heading\n\n```markdown\n# Not a heading\n```\n\nParagraph.\n",
        "fallback",
    )

    assert [anchor.kind for anchor in parsed.anchors] == ["heading", "paragraph"]
    assert parsed.anchors[0].heading_path == ("Real heading",)


def test_explicit_record_sections_are_classified() -> None:
    parsed = parse_markdown(
        "# Project\n\n## Decisions\n\n- Use SQLite.\n\n"
        "## Blockers\n\nNetwork access is unavailable.\n\n"
        "## Conflicts\n\n- Source A and source B disagree.\n",
        "fallback",
    )

    records = [
        (anchor.record_type, anchor.quote)
        for anchor in parsed.anchors
        if anchor.record_type
    ]
    assert records == [
        ("decision", "- Use SQLite."),
        ("blocker", "Network access is unavailable."),
        ("conflict", "- Source A and source B disagree."),
    ]


def test_wikilinks_in_code_or_escaped_literal_syntax_are_ignored() -> None:
    parsed = parse_markdown(
        "# Project\n\n"
        "Real [[Target]], inline `[[Inline]]`, and escaped \\[[Literal]].\n",
        "fallback",
    )

    paragraph = parsed.anchors[1]
    assert paragraph.wikilinks == ("Target",)
    assert "[[Inline]]" not in paragraph.semantic_quote
    assert "[[Literal]]" not in paragraph.semantic_quote
