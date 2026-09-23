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


@pytest.mark.parametrize("marker", ["-", "+", "*", "1.", "12.", "1)", "123456789)"])
@pytest.mark.parametrize("checkbox", [" ", "x", "X"])
def test_task_markers_preserve_metadata_and_evidence(marker: str, checkbox: str) -> None:
    text = f"{marker} [{checkbox}] Do the work. [owner:: Avery] [due:: 2026-10-01]\n"
    parsed = parse_markdown(text, "fallback")
    anchor, = parsed.anchors
    assert anchor.kind == "task" and anchor.task is not None
    assert anchor.task.text == "Do the work."
    assert anchor.task.status == ("open" if checkbox == " " else "completed")
    assert anchor.task.owner == "Avery"
    assert anchor.task.due_date == "2026-10-01"
    assert text[anchor.start_offset:anchor.end_offset] == anchor.quote


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_invalid_frontmatter_has_a_stable_parse_error(newline: str) -> None:
    with pytest.raises(MarkdownParseError, match="Invalid YAML frontmatter"):
        parse_markdown("---\ntitle: [invalid\n---\n\nBody\n".replace("\n", newline), "fallback")


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


@pytest.mark.parametrize("parent_fields", ["", " [owner:: Parent] [due:: 2026-10-01]"])
@pytest.mark.parametrize("child_marker", ["- [ ]", "-", "1."])
def test_list_metadata_does_not_inherit_or_overwrite_from_children(
    parent_fields: str, child_marker: str
) -> None:
    text = (
        f"- [ ] Parent task{parent_fields}\n"
        f"  {child_marker} Child [owner:: Child] [due:: 2026-11-01]\n"
        "     - Grandchild [owner:: Grandchild] [due:: 2026-12-01]\n"
    )

    parsed = parse_markdown(text, "fallback")

    parent = parsed.anchors[0]
    assert parent.task is not None
    assert parent.task.owner == ("Parent" if parent_fields else None)
    assert parent.task.due_date == ("2026-10-01" if parent_fields else None)
    assert parent.metadata == (
        {"owner": "Parent", "due": "2026-10-01"} if parent_fields else {}
    )
    assert parsed.anchors[1].metadata == {"owner": "Child", "due": "2026-11-01"}
    assert parsed.anchors[2].metadata == {"owner": "Grandchild", "due": "2026-12-01"}
    assert parent.quote == text.rstrip()


@pytest.mark.parametrize(
    "content",
    [
        "  [owner:: Parent] [due:: 2026-10-01]\n",
        "[owner:: Parent] [due:: 2026-10-01]\n",
        "\n  [owner:: Parent] [due:: 2026-10-01]\n",
        "  - Child [owner:: Child] [due:: 2026-11-01]\n"
        "\n  [owner:: Parent] [due:: 2026-10-01]\n",
    ],
    ids=["continuation", "lazy-continuation", "paragraph", "after-child"],
)
def test_task_metadata_keeps_direct_continuation_content(content: str) -> None:
    parsed = parse_markdown("- [x] Parent task\n" + content, "fallback")

    task = parsed.anchors[0].task
    assert task is not None
    assert task.text == "Parent task"
    assert task.status == "completed"
    assert task.owner == "Parent"
    assert task.due_date == "2026-10-01"


@pytest.mark.parametrize(
    "code",
    [
        "  ```markdown\n  {content}\n  ```",
        "  ~~~markdown\n  {content}\n  ~~~",
        "  ````markdown\n  ```\n  {content}\n  `````",
        "  ~~~~markdown\n  ~~~\n  {content}\n  ~~~~~",
        "     ```\n     {content}\n     ```",
        "  > ~~~\n  > {content}\n  > ~~~",
        "      {content}",
        "\t  {content}",
        "  ```markdown\n  {content}",
        "  ~~~markdown\n  {content}",
    ],
    ids=[
        "backticks", "tildes", "long-backticks", "long-tildes", "indented-fence",
        "blockquote-fence", "indented-code", "tab-indented-code",
        "unclosed-backticks", "unclosed-tildes",
    ],
)
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_nested_code_is_masked_without_changing_source_offsets(code: str, newline: str) -> None:
    code = code.format(content="[[Hidden]] HiddenAlias [owner:: Hidden] [due:: 2099-01-01]")
    text = (
        "---\ntitle: Example\n---\n\n# Heading\n\n"
        "- [ ] Parent [[Visible]] [owner:: Parent] [due:: 2026-10-01]\n\n"
        + code + "\n"
    ).replace("\n", newline)

    parent = parse_markdown(text, "fallback").anchors[1]

    assert parent.task is not None
    assert parent.task.owner == "Parent"
    assert parent.task.due_date == "2026-10-01"
    assert parent.wikilinks == ("Visible",)
    assert "HiddenAlias" not in parent.semantic_quote
    assert "Hidden" not in parent.semantic_quote
    assert len(parent.semantic_quote) == len(parent.quote)
    assert text[parent.start_offset : parent.end_offset] == parent.quote
    code_start = parent.quote.index(code.replace("\n", newline))
    assert parent.semantic_quote[code_start:] == " " * (len(parent.quote) - code_start)
    visible_start = parent.start_offset + parent.semantic_quote.index("[[Visible]]")
    assert text[visible_start : visible_start + len("[[Visible]]")] == "[[Visible]]"


def test_code_masking_preserves_following_prose_and_nested_task_structure() -> None:
    text = (
        "- [ ] Parent\n"
        "  - [x] Child [owner:: Child]\n\n"
        "    ~~~\n"
        "    [[Hidden]] [owner:: Hidden]\n"
        "    ~~~\n\n"
        "    Child continuation [[ChildTarget]].\n\n"
        "  Parent continuation [[ParentTarget]] [owner:: Parent]\n"
    )

    parent, child = parse_markdown(text, "fallback").anchors

    assert parent.task is not None and parent.task.owner == "Parent"
    assert child.task is not None and child.task.owner == "Child"
    assert child.task.status == "completed"
    assert parent.wikilinks == ("ChildTarget", "ParentTarget")
    assert child.wikilinks == ("ChildTarget",)
    for anchor in (parent, child):
        assert "Hidden" not in anchor.semantic_quote
        assert len(anchor.semantic_quote) == len(anchor.quote)
        assert text[anchor.start_offset : anchor.end_offset] == anchor.quote


def test_task_metadata_ignores_inline_code_and_preserves_literal_links() -> None:
    text = (
        "- [ ] Task [owner:: Parent] `[[Hidden]] [owner:: Hidden] [due:: 2099-01-01]`\n"
        "  Escaped \\[[Literal]] and real [[Visible]].\n"
    )

    anchor = parse_markdown(text, "fallback").anchors[0]

    assert anchor.task is not None
    assert anchor.task.owner == "Parent"
    assert anchor.task.due_date is None
    assert anchor.wikilinks == ("Visible",)
    assert len(anchor.semantic_quote) == len(anchor.quote)


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85"])
def test_unicode_separators_are_not_commonmark_line_boundaries(separator: str) -> None:
    text = f"First{separator}line.\n\nLast paragraph.\n"

    anchors = parse_markdown(text, "note").anchors

    assert [anchor.quote for anchor in anchors] == [f"First{separator}line.", "Last paragraph."]
    for anchor in anchors:
        assert text[anchor.start_offset : anchor.end_offset] == anchor.quote


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85"])
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("frontmatter", [False, True])
@pytest.mark.parametrize("final_newline", [False, True])
def test_commonmark_line_offsets_preserve_headings_tasks_and_frontmatter(
    separator: str, newline: str, frontmatter: bool, final_newline: bool
) -> None:
    heading = f"Heading{separator}continued"
    task_text = f"Do{separator}the work."
    expected_quotes = [
        f"# {heading}",
        f"First{separator}line.",
        f"- [ ] {task_text} [owner:: Avery]",
        "Last paragraph.",
    ]
    text = newline.join(["---", "title: Example", "---", ""]) if frontmatter else ""
    text += (newline * 2).join(expected_quotes) + (newline if final_newline else "")

    parsed = parse_markdown(text, "note")

    assert parsed.title == ("Example" if frontmatter else heading)
    assert parsed.frontmatter == ({"title": "Example"} if frontmatter else {})
    assert [anchor.quote for anchor in parsed.anchors] == expected_quotes
    assert [anchor.kind for anchor in parsed.anchors] == [
        "heading", "paragraph", "task", "paragraph",
    ]
    assert parsed.anchors[2].task is not None
    assert parsed.anchors[2].task.text == task_text
    assert parsed.anchors[2].task.owner == "Avery"
    for anchor in parsed.anchors:
        assert anchor.heading_path == (heading,)
        assert text[anchor.start_offset : anchor.end_offset] == anchor.quote
        assert anchor.start_offset == text.index(anchor.quote)


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_frontmatter_without_body_or_final_newline(newline: str) -> None:
    parsed = parse_markdown(newline.join(["---", "title: Example", "---"]), "fallback")

    assert parsed.title == "Example"
    assert parsed.frontmatter == {"title": "Example"}
    assert parsed.anchors == []


def test_mixed_commonmark_newlines_preserve_source_offsets() -> None:
    text = "---\rtitle: Example\r\n---\n# Heading\r\n\rFirst\u2028line.\n\nLast paragraph.\r"

    parsed = parse_markdown(text, "fallback")

    assert parsed.title == "Example"
    assert [anchor.quote for anchor in parsed.anchors] == [
        "# Heading", "First\u2028line.", "Last paragraph.",
    ]
    for anchor in parsed.anchors:
        assert text[anchor.start_offset : anchor.end_offset] == anchor.quote
