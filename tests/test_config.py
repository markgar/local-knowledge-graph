from pathlib import Path

from kg.config import load_manifest, select_sources


def test_example_manifest_resolves_paths() -> None:
    manifest = load_manifest(Path("corpora/example.yml"))

    selection = select_sources(manifest)

    assert manifest.corpus_id == "example"
    assert manifest.vault_root.is_absolute()
    assert [path.name for path in selection.paths] == ["atlas.md"]
    assert selection.missing == []

