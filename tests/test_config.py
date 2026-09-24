from pathlib import Path

import pytest

from kg.config import ManifestError, load_manifest, read_source, select_sources


@pytest.mark.unit
def test_example_manifest_resolves_paths() -> None:
    manifest = load_manifest(Path("corpora/example.yml"))

    selection = select_sources(manifest)

    assert manifest.corpus_id == "example"
    assert manifest.vault_root.is_absolute()
    assert [path.name for path in selection.paths] == ["atlas.md"]
    assert selection.missing == []


@pytest.mark.unit
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


@pytest.mark.unit
def test_manifest_rejects_symlinked_parent_component(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    target = vault / "target"
    target.mkdir(parents=True)
    (target / "note.md").write_text("# Target\n", encoding="utf-8")
    (vault / "linked").symlink_to(target, target_is_directory=True)
    manifest_path = tmp_path / "corpus.yml"
    manifest_path.write_text(
        "corpus_id: test\n"
        "display_name: Test\n"
        "vault_root: vault\n"
        "database: index.sqlite3\n"
        "include: ['linked/note.md']\n",
        encoding="utf-8",
    )

    with pytest.raises(ManifestError, match="symlink sources are disabled"):
        select_sources(load_manifest(manifest_path))


@pytest.mark.unit
def test_source_bytes_and_metadata_come_from_open_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    source_path = vault / "note.md"
    source_path.write_text("# Stable\n", encoding="utf-8")
    manifest_path = tmp_path / "corpus.yml"
    manifest_path.write_text(
        "corpus_id: test\n"
        "display_name: Test\n"
        "vault_root: vault\n"
        "database: index.sqlite3\n"
        "include: ['note.md']\n",
        encoding="utf-8",
    )
    manifest = load_manifest(manifest_path)
    selected = select_sources(manifest).paths[0]
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda self: (_ for _ in ()).throw(AssertionError("path read raced")),
    )
    monkeypatch.setattr(
        Path,
        "stat",
        lambda self: (_ for _ in ()).throw(AssertionError("path stat raced")),
    )

    opened = read_source(manifest, selected)

    assert opened.content == b"# Stable\n"
    assert opened.observed_mtime


@pytest.mark.unit
def test_secure_read_rejects_source_swapped_outside_vault(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    source_path = vault / "note.md"
    source_path.write_text("# Selected\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    manifest_path = tmp_path / "corpus.yml"
    manifest_path.write_text(
        "corpus_id: test\n"
        "display_name: Test\n"
        "vault_root: vault\n"
        "database: index.sqlite3\n"
        "include: ['note.md']\n"
        "allow_symlinks: true\n",
        encoding="utf-8",
    )
    manifest = load_manifest(manifest_path)
    selected = select_sources(manifest).paths[0]
    source_path.unlink()
    source_path.symlink_to(outside)

    with pytest.raises(ManifestError, match="source escapes vault_root"):
        read_source(manifest, selected)


@pytest.mark.unit
@pytest.mark.parametrize("use_symlink", [False, True])
def test_allow_symlinks_reads_sources_that_remain_inside_vault(
    tmp_path: Path,
    use_symlink: bool,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    target = vault / "target.md"
    target.write_text("# Inside\n", encoding="utf-8")
    source_path = vault / "source.md"
    if use_symlink:
        source_path.symlink_to(target)
    else:
        source_path.write_text("# Inside\n", encoding="utf-8")
    manifest_path = tmp_path / "corpus.yml"
    manifest_path.write_text(
        "corpus_id: test\n"
        "display_name: Test\n"
        "vault_root: vault\n"
        "database: index.sqlite3\n"
        "include: ['source.md']\n"
        "allow_symlinks: true\n",
        encoding="utf-8",
    )
    manifest = load_manifest(manifest_path)
    selected = select_sources(manifest).paths[0]

    opened = read_source(manifest, selected)

    assert opened.content == b"# Inside\n"
