from pathlib import Path

import pytest

from kg.config import ManifestError, load_manifest, select_sources


def test_example_manifest_resolves_paths() -> None:
    manifest = load_manifest(Path("corpora/example.yml"))

    selection = select_sources(manifest)

    assert manifest.corpus_id == "example"
    assert manifest.vault_root.is_absolute()
    assert [path.name for path in selection.paths] == ["atlas.md"]
    assert selection.missing == []


def test_manifest_rejects_symlink_sources_by_default(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "target.md"
    target.write_text("# Target\n", encoding="utf-8")
    (vault / "link.md").symlink_to(target)
    manifest_path = tmp_path / "corpus.yml"
    manifest_path.write_text(
        "corpus_id: test\n"
        "display_name: Test\n"
        "vault_root: vault\n"
        "database: index.sqlite3\n"
        "include: ['link.md']\n",
        encoding="utf-8",
    )

    with pytest.raises(ManifestError, match="symlink sources are disabled"):
        select_sources(load_manifest(manifest_path))
