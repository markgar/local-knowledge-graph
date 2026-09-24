"""Human and machine interface to canonical evidence and knowledge workflows."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable, Sequence
from importlib.resources import files
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
from kg.client.knowledge import Knowledge, record_schema
from kg.client.schema import Schema
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
    help="Canonical evidence and knowledge: setup, add, find, read, record, update and remove. "
    "Use COMMAND --help and kg skill for strategies. Text is not automatically made into facts.",
)
find_app = typer.Typer(
    no_args_is_help=True, help="Find documents, eligible entities, relationships and decisions."
)
app.add_typer(find_app, name="find")
schema_app = typer.Typer(
    no_args_is_help=True,
    help="Prepare external-agent generation context, inspect vocabulary and validate proposals. "
    "Apply is an explicit human-reviewed operator-admin action.",
)
app.add_typer(schema_app, name="schema")
Json = Annotated[bool, typer.Option("--json", help="Return a machine-readable client/1 result.")]
Limit = Annotated[int, typer.Option(min=1, max=200, help="Maximum page entries.")]
After = Annotated[int, typer.Option(min=0, help="Continue from the returned page position.")]
EvidenceOnly = Annotated[
    bool, typer.Option(
        "--evidence-only", help="Save exact evidence without models or search preparation.",
    ),
]


@app.command()
def capabilities(json_output: Json = False) -> None:
    """Inspect authorized schema state and installed workflows without loading models."""
    execute(lambda: Schema(load_profile()).capabilities(), json_output)


@schema_app.command("generate")
def schema_generate(
    file: Annotated[Path | None, typer.Argument()] = None,
    schema: Annotated[bool, typer.Option("--schema")] = False,
    example: Annotated[bool, typer.Option("--example")] = False,
    json_output: Json = False,
) -> None:
    """Prepare exact operator-selected excerpts for EXTERNAL agent interpretation.

    Returns awaiting_agent, not generated definitions. No model, schema or fact writes.
    Use --example for the full sample/proposal recipe; stop for human content approval.
    Already configured corpora use schema show and ordinary additive proposals.
    """
    from kg.models.schema import SchemaSample

    def run() -> Response:
        if sum((file is not None, schema, example)) != 1:
            raise ClientError(
                "invalid_arguments", "Supply FILE, --schema or --example, exactly one.",
            )
        if schema:
            value = SchemaSample.model_json_schema()
            return Response(
                status="complete", message=json.dumps(value, indent=2), result={"schema": value},
            )
        if example:
            text = files("kg.client").joinpath("schema-example.md").read_text(encoding="utf-8")
            return Response(status="complete", message=text, result={"example": text})
        assert file is not None
        return Schema(load_profile()).generate(file)

    execute(run, json_output)


@schema_app.command("show")
def schema_show(
    revision: Annotated[str | None, typer.Option(help="Exact historical schema revision.")] = None,
    json_output: Json = False,
) -> None:
    """Read corpus vocabulary and copy its exact head into record/proposal input."""
    execute(lambda: Schema(load_profile()).show(revision), json_output)


@schema_app.command("history")
def schema_history(
    after_sequence: Annotated[int, typer.Option(min=0)] = 0,
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
    json_output: Json = False,
) -> None:
    """Read bounded revision summaries, not protected examples or actor details."""
    execute(lambda: Schema(load_profile()).history(after_sequence, limit), json_output)


@schema_app.command("change")
def schema_change(revision: str, json_output: Json = False) -> None:
    """Read an authorized change and its recorded approval."""
    execute(lambda: Schema(load_profile()).change(revision), json_output)


@schema_app.command("validate")
def schema_validate(
    file: Annotated[Path | None, typer.Argument()] = None,
    schema: Annotated[bool, typer.Option("--schema")] = False,
    example: Annotated[bool, typer.Option("--example")] = False,
    json_output: Json = False,
) -> None:
    """Validate without mutation or inference. No approval or fact extraction occurs."""
    from kg.models.schema import SchemaProposal

    def run() -> Response:
        if sum((file is not None, schema, example)) != 1:
            raise ClientError(
                "invalid_arguments", "Supply FILE, --schema or --example, exactly one.",
            )
        if schema:
            value = SchemaProposal.model_json_schema()
            return Response(
                status="complete", message=json.dumps(value, indent=2), result={"schema": value},
            )
        if example:
            text = files("kg.client").joinpath("schema-example.md").read_text(encoding="utf-8")
            return Response(status="complete", message=text, result={"example": text})
        assert file is not None
        return Schema(load_profile()).validate(file)

    execute(run, json_output)


@schema_app.command("apply")
def schema_apply(
    file: Path,
    approve_digest: Annotated[
        str, typer.Option(help="Explicitly attest human review of this exact validated digest."),
    ],
    retry_key: Annotated[
        str, typer.Option(help="Stable prepared key; preserve for unknown outcomes."),
    ],
    approval_rationale: Annotated[str, typer.Option(help="Human review/publication rationale.")],
    json_output: Json = False,
) -> None:
    """Trusted local admin apply, not ordinary write permission or secure human authentication.

    Review definitions, examples, reuse/defer and widening Cartesian products first.
    Never auto-approve. Same-OS administrators can invoke this boundary. No models or facts run.
    """
    execute(
        lambda: Schema(load_profile()).apply(
            file, digest=approve_digest, retry_key=retry_key, approval_rationale=approval_rationale,
        ),
        json_output,
    )


def render_knowledge(result: dict[str, Any]) -> None:
    if "decisions" in result:
        qualifier = "exact" if result["exact"] else "lower bound"
        typer.echo(f"Decisions: {result['count']} ({qualifier}; distinct submitted assertion IDs)")
        typer.echo(
            f"Selection complete: {result['selection_complete']}. "
            f"Display complete: {result['display_complete']}."
        )
        for decision in result["decisions"]:
            typer.echo(safe_text(f"{decision['target']}: {decision['text']}"))
            typer.echo(safe_text("Captured support: " + json.dumps(decision["support"])))
            for target in decision["evidence_targets"]:
                typer.echo(safe_text(f"Evidence: {target}"))
            for relationship_id in decision.get("relationship_ids", []):
                typer.echo(safe_text(f"Via relationship: fact:{relationship_id}"))
        for proof in result.get("graph", {}).get("relationships", []):
            assertion = proof["assertion"]
            typer.echo(safe_text(
                f"Relationship fact:{assertion['assertion_id']}: "
                f"entity:{assertion['subject_id']} --{assertion['predicate']}--> "
                f"entity:{assertion['object_entity_id']}"
            ))
            typer.echo(safe_text("Relationship support: " + json.dumps(assertion["support"])))
    entity = result.get("entity")
    if isinstance(entity, dict):
        typer.echo(safe_text(f"{result.get('target')}: {entity['name']} ({entity['entity_type']})"))
        if not entity["is_current"]:
            typer.echo("Historical/inactive entity; not a current fact basis.")
        typer.echo(safe_text("Identifying support: " + json.dumps(entity["witness"])))
        if entity["has_more_support"]:
            typer.echo("More identifying support exists; read the entity's contribution pages.")
    contribution = result.get("contribution")
    if isinstance(contribution, dict):
        typer.echo(safe_text(f"{result.get('target')}: " + json.dumps(contribution["payload"])))
        if not contribution["is_current"]:
            typer.echo("Historical/inactive contribution; not a current fact basis.")
        if contribution.get("withdrawal"):
            typer.echo(safe_text("Withdrawal: " + json.dumps(contribution["withdrawal"])))
        typer.echo(safe_text("Captured support: " + json.dumps(result.get("support"))))
    if result.get("directions"):
        typer.echo(safe_text("Direction: " + ", ".join(result["directions"])))
    for target in result.get("evidence_targets", []):
        typer.echo(safe_text(f"Evidence: {target}"))
    for key in ("query", "inspection", "graph", "write"):
        if key in result and "decisions" not in result:
            typer.echo(safe_text(f"{key.capitalize()}:\n" + json.dumps(result[key], indent=2)))
    if result.get("display_truncated"):
        typer.echo("Display is incomplete; not all retained counted members are shown.")
    for entry in result.get("entries", []):
        if isinstance(entry, dict) and ("entity" in entry or "contribution" in entry):
            render_knowledge(entry)
    if "selected" in result:
        render_knowledge(result["selected"])


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
        if (
            result.get("interface_version") in {"knowledge-schema/1", "schema-generation/1"}
            or "workflows" in result
            or "apply" in result
            or {"request_id", "retry_key", "proposal_digest"} <= result.keys()
        ):
            typer.echo(safe_text(json.dumps(result, indent=2)))
        render_knowledge(result)
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
                if entry is not result and isinstance(evidence, dict):
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
            if result.get("interface_version") == "knowledge-schema/1":
                typer.echo(f"More revisions: --after-sequence {result.get('next_after_sequence')}")
            else:
                typer.echo(f"More entries: --after {result.get('next_after')}")
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
            "No downloads. Without approval, exact reads/removal and --evidence-only "
            "add/update work; ordinary add/update/search stop.",
        ),
    ] = False,
    schema_preset: Annotated[
        str | None, typer.Option(
            help="Explicit example vocabulary: personal/1. Default: no schema.",
        ),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", help="Accept local defaults without prompting.")
    ] = False,
    json_output: Json = False,
) -> None:
    """Set up a schema-free local profile. Example: kg setup --yes --json.

    --schema-preset personal/1 explicitly installs the people/project example.
    Attaching never changes an existing corpus's vocabulary.
    """

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
            schema_preset=schema_preset,
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
def add(file: Path, evidence_only: EvidenceOnly = False, json_output: Json = False) -> None:
    """Save exact UTF-8 text and prepare search. Example: kg add meeting.md.

    Each invocation creates a document. An interrupted/uncertain write can leave
    saved data; manual resubmission may duplicate it.
    """
    execute(lambda: Documents(load_profile()).save(file, evidence_only=evidence_only), json_output)


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
        bool, typer.Option("--history", help="Document history or historical knowledge inspection.")
    ] = False,
    after: After = 0,
    limit: Limit = 100,
    json_output: Json = False,
) -> None:
    """Read document:ID, evidence:REFERENCE, entity:ID or fact:ID.

    Document reads return exact anchor pages and copy-ready support objects.
    Knowledge reads include authorized support/contributions, not inferred facts.
    Continue with --after NEXT_AFTER. Document history returns document:ID@STATE targets
    for reading old evidence; historical evidence still requires current access.
    """
    execute(
        lambda: (
            Knowledge(load_profile())
            if target.startswith(("entity:", "fact:"))
            else Documents(load_profile())
        ).read(target, history=history, after=after, limit=limit),
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
    evidence_only: EvidenceOnly = False,
    json_output: Json = False,
) -> None:
    """Revise document:ID, preserving metadata/history, then prepare search."""

    def run() -> Response:
        documents = Documents(load_profile())
        previous = documents.document(document)
        expected = expected_state(previous.state_version, expect, document, json_output)
        return documents.save(
            file, previous=previous, expected=expected, evidence_only=evidence_only,
        )

    execute(run, json_output)


@app.command()
def remove(
    document: str,
    expect: Annotated[
        str | None, typer.Option(help="Exact expected state from read output.")
    ] = None,
    confirm: Annotated[
        bool,
        typer.Option("--confirm", help="Confirm source deactivation or owned fact withdrawal."),
    ] = False,
    json_output: Json = False,
) -> None:
    """Deactivate document:ID or withdraw exact owned assertion fact:ID; retain history.

    Fact withdrawal requires --confirm in noninteractive use, not --expect.
    No physical purge, entity deletion or merging is performed.
    """

    def run() -> Response:
        if document.startswith("fact:"):
            if expect is not None:
                raise ClientError("invalid_arguments", "--expect applies only to documents.")
            if not confirm:
                if json_output or not interactive():
                    raise ClientError("confirmation_required", "Withdrawal requires --confirm.")
                typer.confirm(f"Withdraw {safe_text(document)}? History is retained.", abort=True)
            return Knowledge(load_profile()).remove(document)
        documents = Documents(load_profile())
        previous = documents.document(document)
        expected = expected_state(previous.state_version, expect, document, json_output)
        if not confirm:
            if json_output or not interactive():
                raise ClientError("confirmation_required", "Removal requires --confirm.")
            typer.confirm(f"Deactivate {safe_text(document)}? History is retained.", abort=True)
        return documents.remove(previous, expected)

    execute(run, json_output)


@find_app.command("entities")
def find_entities(
    name: Annotated[str | None, typer.Argument(help="Exact name or alias; omit to list.")] = None,
    after: After = 0,
    limit: Limit = 100,
    json_output: Json = False,
) -> None:
    """List eligible entities with IDs, types and identifying support.

    Matching is exact case-sensitive names/aliases, not semantic search.
    Empty/incomplete results do not prove an entity is new. List/page entities
    or inspect source evidence before deciding to create one.
    A page is not a uniqueness claim. Follow --after until complete, or select entity:ID.
    """
    execute(lambda: Knowledge(load_profile()).entities(name, after=after, limit=limit), json_output)


@find_app.command("relationships")
def find_relationships(
    entity: str,
    after: After = 0,
    limit: Limit = 100,
    json_output: Json = False,
) -> None:
    """Read entity-valued assertions incident to an exact entity:ID or unique exact name/alias.

    Includes outgoing and incoming relationships and their evidence. --limit bounds
    the underlying assertion page: follow --after even when a filtered page is empty.
    """
    execute(
        lambda: Knowledge(load_profile()).relationships(entity, after=after, limit=limit),
        json_output,
    )


@find_app.command("decisions")
def find_decisions(
    entity: str,
    through: Annotated[
        str | None, typer.Option(help="Registered predicate: owns outgoing; ^owns incoming.")
    ] = None,
    limit: Annotated[
        int, typer.Option(min=1, max=1000, help="Maximum displayed decisions.")
    ] = 1000,
    json_output: Json = False,
) -> None:
    """Find explicit decisions about an exact entity:ID or unique exact name/alias.

    Starter vocabulary: person, project; owns (person -> project), decision (string).
    Direct queries use canonical SQLite. --through uses optional Ladybug 0.20.4,
    supported on macOS 15+ ARM64 / Python 3.12. Native code runs in-process and can
    crash the host; its 256 MiB buffer is not an RSS cap. Each call builds a fresh
    disposable graph. No models, planner, arbitrary Cypher or cross-command handles.
    Counts are distinct submitted IDs; partial/budget outcomes are not exact totals.
    Output includes decision text, fact targets, captured support and count/display
    completeness. Direct text uses separate authorized reads and a final retained
    membership check, not an atomic snapshot. Each read has its existing budget;
    large displays may outlive the five-minute retained set and fail explicitly.
    """
    execute(
        lambda: Knowledge(load_profile()).decisions(entity, through=through, limit=limit),
        json_output,
    )


@app.command()
def record(
    file: Annotated[Path | None, typer.Argument(help="Grounded UTF-8 JSON submission.")] = None,
    schema: Annotated[bool, typer.Option("--schema", help="Print the record JSON Schema.")] = False,
    example: Annotated[
        bool, typer.Option("--example", help="Print the annotated input recipe.")
    ] = False,
    retry_key: Annotated[
        str | None, typer.Option(help="Persisted retry identity; required with FILE.")
    ] = None,
    json_output: Json = False,
) -> None:
    """Record supported knowledge, explicitly creating local entities or reusing stored IDs.

    Run kg record --example for copy-ready evidence/entity reference instructions;
    kg record --schema for native changes or named captured-support shorthand.
    Copy entity reference objects into record; use target strings as command arguments.
    Starter vocabulary: person,
    project, owns (person -> project), decision (string). Support must be copied
    from reads without replacing old states. No endpoints are implicitly created.
    FILE requires --retry-key. Retry an unknown outcome with the exact input/key.
    Identity is independent of classification; select a supported claim explicitly.
    """

    def run() -> Response:
        if sum((file is not None, schema, example)) != 1:
            raise ClientError(
                "invalid_arguments", "Supply FILE, --schema or --example, exactly one."
            )
        if schema:
            value = record_schema()
            return Response(
                status="complete", message=json.dumps(value, indent=2), result={"schema": value}
            )
        if example:
            text = files("kg.client").joinpath("record-example.md").read_text(encoding="utf-8")
            return Response(status="complete", message=text, result={"example": text})
        assert file is not None
        if retry_key is None:
            raise ClientError("invalid_arguments", "FILE requires a persisted --retry-key.")
        return Knowledge(load_profile()).record(file, retry_key=retry_key)

    execute(run, json_output)


@app.command()
def classifications(
    entity: str,
    history: Annotated[
        bool, typer.Option(help="Read authorized immutable selection history.")
    ] = False,
    after_event_id: Annotated[str | None, typer.Option(help="Opaque history cursor.")] = None,
    limit: Limit = 100,
    review_claim: Annotated[
        list[str] | None, typer.Option(help="Explicit fact:ID subset; repeat for each claim.")
    ] = None,
    review_empty: Annotated[
        bool, typer.Option(help="Explicit empty subset review, for clearing a selection.")
    ] = False,
    json_output: Json = False,
) -> None:
    """Review classification claims before an explicit selection-only kg record."""
    def run() -> Response:
        if (
            history and (review_claim is not None or review_empty)
            or review_empty and review_claim is not None
            or not history and after_event_id is not None
        ):
            raise ClientError(
                "invalid_arguments", "Choose history, complete review or one subset mode.",
            )
        if review_claim is not None and any(not c.startswith("fact:") for c in review_claim):
            raise ClientError("invalid_target", "--review-claim requires exact fact:ID targets.")
        claim_ids = (
            tuple(c.removeprefix("fact:") for c in review_claim)
            if review_claim is not None else () if review_empty else None
        )
        return Knowledge(load_profile()).classifications(
            entity, history=history, after_event_id=after_event_id,
            limit=limit, claim_ids=claim_ids,
        )
    execute(run, json_output)


@app.command()
def withdraw_classification(
    fact: str,
    retry_key: Annotated[str, typer.Option(help="Persisted retry identity.")],
    json_output: Json = False,
) -> None:
    """Terminally withdraw one caller-owned classification claim; never erase identity."""
    execute(
        lambda: Knowledge(load_profile()).withdraw_classification(fact, retry_key=retry_key),
        json_output,
    )


@app.command()
def skill(json_output: Json = False) -> None:
    """Print the installed agent strategy skill (no profile required).

    Save the text output as SKILL.md in your agent's use-knowledge-graph skill folder.
    """

    def run() -> Response:
        text = files("kg.client").joinpath("SKILL.md").read_text(encoding="utf-8")
        return Response(status="complete", message=text, result={"skill": text})

    execute(run, json_output)
