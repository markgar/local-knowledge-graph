"""Human and machine interface to the canonical document workflow."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError
from typer.core import TyperGroup

from kg.client.config import (
    ClientError,
    configure,
    default_store,
    load_profile,
    profile_path,
)
from kg.client.documents import Documents, Response
from kg.evidence.errors import EvidenceServiceError

# Typer 0.26+ vendors Click; earlier supported versions use the standalone package.
try:
    from typer._click.exceptions import ClickException
except ImportError:
    from click import ClickException  # type: ignore[assignment]


class ClientGroup(TyperGroup):
    """Keep parser errors in the same JSON envelope as command outcomes."""

    def main(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        windows_expand_args: bool = True,
        **extra: Any,
    ) -> Any:
        arguments = list(sys.argv[1:] if args is None else args)
        options = arguments[: arguments.index("--")] if "--" in arguments else arguments
        machine = "--json" in options
        try:
            result = super().main(
                args=arguments,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=False if machine else standalone_mode,
                windows_expand_args=windows_expand_args,
                **extra,
            )
        except ClickException as error:
            if not machine:
                raise
            typer.echo(
                Response(
                    status="rejected",
                    code="invalid_arguments",
                    message=error.format_message(),
                    exit_code=error.exit_code,
                ).model_dump_json(),
            )
            result = error.exit_code
        if machine and standalone_mode:
            raise SystemExit(result)
        return result


app = typer.Typer(
    cls=ClientGroup,
    no_args_is_help=True,
    help="Canonical local evidence: setup, add, find documents, read, update and remove. "
    "Use COMMAND --help for examples. Text is not automatically extracted into facts.",
)
find_app = typer.Typer(no_args_is_help=True, help="Find supplied document evidence.")
app.add_typer(find_app, name="find")
Json = Annotated[bool, typer.Option("--json", help="Return a machine-readable client/1 result.")]
Limit = Annotated[int, typer.Option(min=1, max=200, help="Maximum page entries.")]
After = Annotated[int, typer.Option(min=0, help="Continue from the returned page position.")]


def safe_text(text: str) -> str:
    return "".join(
        char if char in "\n\t" or char.isprintable() else f"\\u{ord(char):04x}" for char in text
    )


def interactive() -> bool:
    return sys.stdin.isatty()


def render(response: Response, machine: bool) -> None:
    if machine:
        typer.echo(response.model_dump_json())
    else:
        typer.echo(safe_text(response.message))
        result = response.result
        for key in ("profile_path", "target", "state"):
            if isinstance(result.get(key), str):
                typer.echo(f"{key.replace('_', ' ').capitalize()}: {safe_text(str(result[key]))}")
        document = result.get("document")
        if isinstance(document, dict):
            typer.echo(f"State: {document.get('state_version')}")
        entries = result.get("entries")
        if isinstance(result.get("evidence"), dict):
            entries = [result]
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                evidence = entry.get("evidence")
                if isinstance(evidence, dict):
                    if "score" in entry:
                        typer.echo(
                            safe_text(
                                f"\n{entry.get('document')} (score {entry['score']})\n"
                                f"{evidence.get('quote', '')}",
                            )
                        )
                    if not evidence.get("is_current_support"):
                        typer.echo("Historical/inactive support; not a current fact basis.")
                if entry is not result:
                    typer.echo(safe_text(f"Evidence: {entry.get('target')}"))
        page = result.get("page")
        if isinstance(page, dict):
            targets = result.get("targets")
            if isinstance(targets, list):
                for target in targets:
                    typer.echo(safe_text(str(target)))
            if page.get("has_more"):
                typer.echo(f"More history: --after {page.get('next_after_sequence')}")
        if result.get("has_more"):
            typer.echo(f"More evidence: --after {result.get('next_after')}")
        search = result.get("search")
        if isinstance(search, dict) and any(
            search.get(key) for key in ("lexical_truncated", "dense_truncated", "fusion_truncated")
        ):
            typer.echo(
                "Search candidate selection was truncated; these are ranked hits, not all data."
            )
        if response.code:
            typer.echo(f"Reason: {response.code}")
    if response.exit_code:
        raise typer.Exit(response.exit_code)


def execute(operation: Callable[[], Response], machine: bool) -> None:
    try:
        response = operation()
    except typer.Abort:
        response = Response(
            status="rejected", message="Cancelled. No operation submitted.", exit_code=2
        )
    except KeyboardInterrupt:
        response = Response(
            status="failed",
            code="interrupted",
            message="Operation interrupted. No completed outcome is reported.",
            exit_code=6,
        )
    except ClientError as error:
        response = Response(
            status="rejected",
            code=error.code,
            message=str(error),
            exit_code=2,
        )
    except EvidenceServiceError as error:
        response = Response(
            status="failed",
            code=error.failure.code,
            message=f"Operation failed: {error.failure.code}.",
            result={"failure": error.failure.model_dump(mode="json")},
            exit_code=3,
        )
    except (ValidationError, UnicodeError):
        response = Response(
            status="rejected",
            code="invalid_input",
            message="Invalid input or profile. Check command help and required UTF-8/value shapes.",
            exit_code=2,
        )
    except OSError:
        response = Response(
            status="failed",
            code="io_error",
            message="Local file operation failed. Check paths, permissions and free space. "
            "Setup may have partially completed; existing stores are never reset.",
            exit_code=6,
        )
    except Exception:
        logging.getLogger(__name__).error("Unexpected CLI failure; no success is assumed.")
        response = Response(
            status="failed",
            code="internal_error",
            message="Unexpected failure. No successful outcome can be assumed.",
            exit_code=6,
        )
    render(response, machine)


@app.command()
def setup(
    store: Annotated[
        Path | None, typer.Option(help="New store path; default is persistent local data.")
    ] = None,
    attach: Annotated[
        Path | None,
        typer.Option(
            help="Attach using a previously generated profile JSON; never initializes its store.",
        ),
    ] = None,
    model_cache: Annotated[
        Path | None, typer.Option(help="Existing approved model cache directory.")
    ] = None,
    approve_models: Annotated[
        bool,
        typer.Option(
            "--approve-models",
            help="Approve pinned cached models (GTE includes trusted model code). "
            "No downloads. Without approval, exact reads/removal work but add/update/search stop.",
        ),
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", help="Accept local defaults without prompting.")
    ] = False,
    json_output: Json = False,
) -> None:
    """Set up one personal profile. Example: kg setup (guided); kg setup --yes --json."""

    def run() -> Response:
        nonlocal store, approve_models
        if attach is not None and store is not None:
            raise ClientError(
                "invalid_option", "Choose a new store or an existing profile to attach."
            )
        if not yes and not json_output:
            if not interactive():
                raise ClientError("confirmation_required", "Use --yes for noninteractive setup.")
            if attach is None:
                store = Path(
                    typer.prompt("New evidence store", default=str(store or default_store()))
                )
            if not approve_models:
                approve_models = typer.confirm(
                    "Approve pinned cached local models, including GTE trusted model code? "
                    "No models will be downloaded",
                    default=False,
                )
            typer.confirm("Save this personal local profile?", abort=True)
        profile = configure(
            store or default_store(),
            attach=attach,
            models_approved=approve_models,
            cache=model_cache,
        )
        return Response(
            status="complete",
            message="Local profile configured. Models have not been loaded.",
            result={
                "profile_path": str(profile_path()),
                "profile": profile.model_dump(mode="json"),
            },
        )

    execute(run, json_output)


@app.command()
def add(file: Path, json_output: Json = False) -> None:
    """Save exact UTF-8 text and prepare search. Example: kg add meeting.md.

    Each invocation creates a document. An interrupted/uncertain write can leave
    saved data; manual resubmission may duplicate it.
    """
    execute(lambda: Documents(load_profile()).save(file), json_output)


@find_app.command("documents")
def find_documents(
    text: str,
    limit: Annotated[int, typer.Option(min=1, max=100, help="Maximum ranked passage hits.")] = 5,
    json_output: Json = False,
) -> None:
    """Find ranked evidence, not generated answers. Example: kg find documents 'release plans'."""
    execute(lambda: Documents(load_profile()).search(text, limit), json_output)


@app.command()
def read(
    target: str,
    history: Annotated[
        bool, typer.Option("--history", help="Page through document state history.")
    ] = False,
    after: After = 0,
    limit: Limit = 100,
    json_output: Json = False,
) -> None:
    """Read document:ID or a returned evidence:REFERENCE.

    Document reads return exact anchor pages and copy-ready support objects.
    Continue with --after NEXT_AFTER. History returns document:ID@STATE targets
    for reading old evidence; historical evidence still requires current access.
    """
    execute(
        lambda: Documents(load_profile()).read(target, history=history, after=after, limit=limit),
        json_output,
    )


def expected_state(current: str, expected: str | None, target: str, machine: bool) -> str:
    if expected is not None:
        return expected
    if machine or not interactive():
        raise ClientError("expected_state_required", "Read the document and supply --expect STATE.")
    typer.confirm(f"Change {safe_text(target)} at state {safe_text(current)}?", abort=True)
    return current


@app.command()
def update(
    document: str,
    file: Path,
    expect: Annotated[
        str | None, typer.Option(help="Exact expected state from read output.")
    ] = None,
    json_output: Json = False,
) -> None:
    """Revise document:ID, preserving metadata/history, then prepare search."""

    def run() -> Response:
        documents = Documents(load_profile())
        previous = documents.document(document)
        expected = expected_state(previous.state_version, expect, document, json_output)
        return documents.save(file, previous=previous, expected=expected)

    execute(run, json_output)


@app.command()
def remove(
    document: str,
    expect: Annotated[
        str | None, typer.Option(help="Exact expected state from read output.")
    ] = None,
    confirm: Annotated[
        bool, typer.Option("--confirm", help="Confirm logical source deactivation.")
    ] = False,
    json_output: Json = False,
) -> None:
    """Deactivate document:ID; retain exact source/history."""

    def run() -> Response:
        documents = Documents(load_profile())
        previous = documents.document(document)
        expected = expected_state(previous.state_version, expect, document, json_output)
        if not confirm:
            if json_output or not interactive():
                raise ClientError("confirmation_required", "Removal requires --confirm.")
            typer.confirm(f"Deactivate {safe_text(document)}? History is retained.", abort=True)
        return documents.remove(previous, expected)

    execute(run, json_output)
