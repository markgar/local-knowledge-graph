from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kg.config import load_manifest
from kg.db import Database
from kg.ids import digest
from kg.ingest import IngestService
from kg.legacy_cli import app
from kg.retrieval import RetrievalService
from kg.retrieval.context import contextual_passage_text
from kg.retrieval.service import RecordNotFoundError

SOURCE = (
    "Preamble.\n\n"
    "# Project\n\n"
    "Overview.\n\n"
    "## Review\n\n"
    "Waiting for approval.\n\n"
    "#### Detail\n\n"
    "Follow up tomorrow.\n\n"
    "## Review\n\n"
    "A separate review.\n\n"
    "## Next\n\n"
    "Unrelated material.\n"
)


def _corpus(tmp_path: Path, text: str = SOURCE) -> tuple[Path, RetrievalService]:
    (tmp_path / "note.md").write_text(text, encoding="utf-8")
    path = tmp_path / "corpus.yml"
    path.write_text(
        "corpus_id: context\n"
        "display_name: Context\n"
        "vault_root: .\n"
        "database: index.sqlite3\n"
        "include: ['note.md']\n",
        encoding="utf-8",
    )
    manifest = load_manifest(path)
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)
    return path, RetrievalService(database, manifest.corpus_id)


@pytest.mark.service
def test_context_keeps_section_boundaries_and_exact_evidence(tmp_path: Path) -> None:
    _, retrieval = _corpus(tmp_path)
    hit = retrieval.search("approval")[0]
    result = retrieval.source_context(hit.anchor_id)
    assert result.selected == retrieval.source_range(hit.anchor_id)
    assert result.section_heading is not None
    assert result.section_heading.quote == "## Review"
    assert [anchor.quote for anchor in result.anchors] == [
        "## Review", "Waiting for approval.", "#### Detail", "Follow up tomorrow."
    ]
    assert result.total_anchors == 4
    assert result.truncated is False
    for anchor in result.anchors:
        assert anchor == retrieval.source_range(anchor.anchor_id)
        assert anchor.source_revision_id == hit.source_revision_id
        assert SOURCE[anchor.start_offset : anchor.end_offset] == anchor.quote
        assert digest(anchor.quote) == anchor.quote_hash

    repeated = retrieval.source_context(retrieval.search("separate")[0].anchor_id)
    assert [anchor.quote for anchor in repeated.anchors] == [
        "## Review", "A separate review."
    ]
    child = retrieval.source_context(retrieval.search("tomorrow")[0].anchor_id)
    assert [anchor.quote for anchor in child.anchors] == [
        "#### Detail", "Follow up tomorrow."
    ]
    heading = retrieval.source_context(result.section_heading.anchor_id)
    assert heading.anchors == result.anchors


@pytest.mark.service
def test_preamble_and_headingless_document(tmp_path: Path) -> None:
    _, retrieval = _corpus(tmp_path)
    result = retrieval.source_context(retrieval.search("Preamble")[0].anchor_id)
    assert result.section_heading is None
    assert [anchor.quote for anchor in result.anchors] == ["Preamble."]

    other = tmp_path / "headingless"
    other.mkdir()
    _, retrieval = _corpus(other, "First paragraph.\n\nSecond paragraph.\n")
    result = retrieval.source_context(retrieval.search("Second")[0].anchor_id)
    assert result.section_heading is None
    assert len(result.anchors) == 2


@pytest.mark.service
@pytest.mark.parametrize(
    "text",
    [
        "- ## Section\n\n  Content.\n",
        "- ## Section\n",
        "# Parent\n\n- ## Section\n\n  Content.\n\n## Next\n\nOther.\n",
        "- Outer\n  - ## Section\n\n    Content.\n",
    ],
)
def test_context_includes_overlapping_list_and_heading_hits(
    tmp_path: Path, text: str,
) -> None:
    _, retrieval = _corpus(tmp_path, text)
    for passage in retrieval.current_passages():
        for limit in (1, 50):
            result = retrieval.source_context(passage.anchor_id, max_anchors=limit)
            assert result.selected in result.anchors
            assert result.total_anchors >= len(result.anchors) >= 1
            assert all(
                text[anchor.start_offset : anchor.end_offset] == anchor.quote
                for anchor in result.anchors
            )


@pytest.mark.service
def test_context_is_bounded_around_selected_anchor(tmp_path: Path) -> None:
    text = "# Large\n\n" + "\n\n".join(f"Paragraph {index}." for index in range(100))
    _, retrieval = _corpus(tmp_path, text)
    for query in ("0", "50", "99"):
        selected = retrieval.search(query)[0]
        result = retrieval.source_context(selected.anchor_id, max_anchors=5)
        assert len(result.anchors) == 5
        assert result.selected in result.anchors
        assert result.total_anchors == 101
        assert result.truncated is True
        assert result.section_heading is not None
        assert result.section_heading.quote == "# Large"
        single = retrieval.source_context(selected.anchor_id, max_anchors=1)
        assert single.anchors == [single.selected]
    for invalid in (0, -1, 201):
        with pytest.raises(ValueError, match="max_anchors"):
            retrieval.source_context(selected.anchor_id, max_anchors=invalid)


@pytest.mark.service
def test_context_uses_stored_revision_and_enforces_corpus_scope(tmp_path: Path) -> None:
    path, retrieval = _corpus(tmp_path)
    hit = retrieval.search("approval")[0]
    original = retrieval.source_context(hit.anchor_id)
    manifest = load_manifest(path)
    (tmp_path / "note.md").write_text("# Changed\n\nNew content.\n", encoding="utf-8")
    IngestService(Database(manifest.database)).ingest(manifest)
    (tmp_path / "note.md").unlink()
    historical = retrieval.source_context(hit.anchor_id)
    assert not historical.selected.is_current
    assert [anchor.quote for anchor in historical.anchors] == [
        anchor.quote for anchor in original.anchors
    ]
    assert all(not anchor.is_current for anchor in historical.anchors)
    with pytest.raises(RecordNotFoundError):
        RetrievalService(Database(manifest.database), "another-corpus").source_context(
            hit.anchor_id
        )
    with pytest.raises(RecordNotFoundError):
        retrieval.source_context("missing")


@pytest.mark.functional
def test_context_cli_and_capability(tmp_path: Path) -> None:
    path, retrieval = _corpus(tmp_path)
    runner = CliRunner()
    args = ["--manifest", str(path), "--format", "json"]
    hit = retrieval.search("approval")[0]
    result = runner.invoke(app, ["source-context", hit.anchor_id, "--max-anchors", "2", *args])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["selected"]["quote"] == hit.quote
    assert payload["truncated"] is True
    assert len(payload["anchors"]) == 2
    missing = runner.invoke(app, ["source-context", "missing", *args])
    assert missing.exit_code == 2
    assert json.loads(missing.stderr)["error"] == "source_context_not_found"
    invalid = runner.invoke(app, ["source-context", hit.anchor_id, "--max-anchors", "0", *args])
    assert invalid.exit_code != 0
    capabilities = json.loads(runner.invoke(app, ["capabilities", "--format", "json"]).stdout)
    assert "source-context" in {tool["command"] for tool in capabilities["tools"]}
    for mode in ("strict", "natural"):
        invalid_mode = runner.invoke(
            app, ["search", "approval", "--query-mode", mode, "--contextual", *args]
        )
        assert invalid_mode.exit_code == 2
        assert json.loads(invalid_mode.stderr)["error"] == "invalid_query"


@pytest.mark.service
def test_contextual_text_keeps_quote_verbatim_and_handles_missing_metadata() -> None:
    quote = "  A precise quote.\n"
    assert contextual_passage_text(quote, "Title", ["Parent", "Child"]) == (
        "Title: Title\n\nHeading: Parent / Child\n\n" + quote
    )
    assert contextual_passage_text(quote, None, []) == quote
