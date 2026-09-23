from __future__ import annotations

from collections.abc import Sequence

CONTEXTUAL_SOURCE_TEXT_VERSION = "title-heading-passage-v1"


def contextual_passage_text(
    quote: str,
    title: str | None,
    heading_path: Sequence[str],
) -> str:
    """Build retrieval text without altering the canonical evidence quote."""
    parts = []
    if title:
        parts.append(f"Title: {title}")
    if heading_path:
        parts.append(f"Heading: {' / '.join(heading_path)}")
    parts.append(quote)
    return "\n\n".join(parts)
