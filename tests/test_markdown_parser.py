import pytest

from kg.markdown import MarkdownParseError, parse_markdown


def test_parser_preserves_structure_and_offsets() -> None:
    text = """---
title: Example
---

# Heading

Paragraph with [[Target]].

- [ ] Do the work.
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


def test_invalid_frontmatter_has_a_stable_parse_error() -> None:
    with pytest.raises(MarkdownParseError, match="Invalid YAML frontmatter"):
        parse_markdown("---\ntitle: [invalid\n---\n\nBody\n", "fallback")
