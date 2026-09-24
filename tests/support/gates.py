"""Portable test selection and import-boundary values; no product imports."""

from __future__ import annotations

import importlib.abc
import json
import os
import sys
import tempfile
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
LAYERS = frozenset({"unit", "service", "functional", "process", "native", "acceptance"})
FORBIDDEN = frozenset({"ladybug", "sentence_transformers", "transformers", "torch"})
NATIVE_CONTEXT = "KG_TEST_REQUIRES_NATIVE"


def cases(layer: str, *selectors: str) -> dict[str, str]:
    return {f"tests/{selector}": layer for selector in selectors}


CORE = {
    **cases(
        "service",
        "test_evidence_database.py::test_fresh_format_reopen_and_transaction",
        "test_evidence_service.py::test_identity_exact_content_noop_and_restore",
        "test_evidence_service.py::test_fault_rolls_back_content_state_and_receipt",
        "test_evidence_retries.py::test_revoked_retry_never_returns_receipt",
        "test_evidence_acceptance.py::test_a01_actual_workload_indices_and_isolated_copy",
        "test_evidence_acceptance.py::test_a02_exact_recipe_and_rejected_ranges",
        "test_knowledge_registry.py::test_registration_reopen_order_independence_and_no_fact_mutations",
        "test_knowledge_enrichment.py::test_failed_last_change_rolls_back_entities_seeds_and_key",
    ),
    **cases(
        "functional",
        "test_canonical_cli.py::test_document_journey_exact_evidence_history_and_search",
        "test_canonical_cli.py::test_module_entrypoint_outside_checkout",
    ),
}
AREAS = {
    "evidence": cases(
        "service",
        "test_evidence_schema.py::test_copied_manifest_cannot_hide_constraint_changes",
        "test_evidence_service.py::test_original_python_types_rejected_before_batch_item_zero",
        "test_evidence_admission.py::test_rechecks_reject_tampering_even_with_restored_schema_cookie",
    ),
    "schema": {
        **cases(
            "service",
            "test_schema_evolution.py::test_injected_storage_failure_rolls_back_revision_head_receipt",
            "test_schema_evolution.py::test_wrong_admin_identity_and_forged_approval_cannot_apply",
        ),
        **cases(
            "process",
            "test_schema_evolution.py::test_schema_commit_invalidates_retained_query_but_fresh_query_keeps_authored_members",
        ),
        **cases(
            "functional",
            "test_schema_cli.py::test_discover_validate_approve_apply_record_read_and_retry",
        ),
    },
    "knowledge": {
        **cases(
            "service",
            "test_knowledge_enrichment.py::test_full_conjunction_and_hidden_endpoint_prevent_alias_activation",
        ),
        **cases(
            "process",
            "test_query_revalidation.py::test_cached_revalidation_rechecks_complete_supplied_bundle",
            "test_query_revalidation.py::test_withdrawal_checked_before_warm_exact_witness_cache",
        ),
    },
    "indexing": cases(
        "service",
        "test_index_search.py::test_actual_full_pipeline_exact_citation_and_report",
        "test_index_search.py::test_initial_authorization_precedes_models_and_denied_scope_not_silently_reduced",
        "test_index_search.py::test_provider_faults_no_fallback_or_projection_mutation",
    ),
    "query": cases(
        "process",
        "test_query.py::test_same_content_edit_restore_is_still_state_changed",
        "test_query.py::test_real_process_protocol_and_failures_never_release",
        "test_query_search.py::test_real_ranked_output_five_charges_exact_quotes_and_linked_stage_facts",
    ),
    "processing": {
        **cases(
            "service",
            "test_processing_control.py::test_fail_checks_actual_current_authority_and_claim",
        ),
        **cases("process", "test_processing_control.py::test_two_processes_cannot_claim_same_job"),
    },
    "graph": cases(
        "service",
        "test_graph_session.py::test_external_commit_invalidates_warm_without_rebuild",
        "test_graph_observer.py::test_revocation_after_snapshot_is_forbidden_at_fence",
        "test_graph_export.py::test_complete_mapping_proof_identity_and_exact_semantic_charge",
    ),
    "cli": cases(
        "functional",
        "test_canonical_cli.py::test_saved_receipt_survives_preparation_failure",
        "test_canonical_cli.py::test_parser_errors_use_json_envelope",
        "test_canonical_cli.py::test_every_help_level_is_model_free",
        "test_schema_cli.py::test_discover_validate_approve_apply_record_read_and_retry",
    ),
    "demo": {
        **cases("service", "test_product_search.py::test_unavailable_projection_never_degrades"),
        **cases(
            "functional",
            "test_benchmark_continuity.py::test_private_worker_preserves_quote_opt_in_and_errors",
        ),
    },
    "evaluation": cases(
        "functional",
        "test_benchmark_continuity.py::test_work_memory_pins_actual_backend_and_retains_journal",
        "test_work_memory_benchmark.py::test_missing_citation_is_not_full_credit",
    ),
    "validation": {
        **cases(
            "unit",
            "test_ci_scope.py::test_workflow_is_manual_and_gates_matrix_before_installation",
            "test_ci_scope.py::test_workflow_defaults_to_local_python_with_optional_full_matrix",
        ),
        "tests/test_test_gates.py": "functional",
    },
}
RELEASE_OBLIGATIONS = [
    "not_run: isolated sdist+wheel packaging validation",
    "not_run: complete portable integration and process/race matrices",
    "not_run: all original capacity and authored evaluation acceptance",
    "not_run: required supported-host Ladybug tests and varied-10000/session examples",
    "not_run: applicable approved-cache real-model parity",
]


def synthetic(module: ModuleType) -> bool:
    spec = getattr(module, "__spec__", None)
    return not (
        getattr(module, "__file__", None)
        or getattr(module, "__loader__", None)
        or (spec and (spec.origin or spec.loader))
    )


class RuntimeGuard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in FORBIDDEN:
            raise RuntimeError(f"Test boundary forbids real runtime import: {fullname}")
        return None

    def install(self) -> None:
        for name, module in tuple(sys.modules.items()):
            if name.split(".")[0] in FORBIDDEN and module is not None and not synthetic(module):
                raise RuntimeError(f"Test boundary found preloaded real runtime: {name}")
        if self not in sys.meta_path:
            sys.meta_path.insert(0, self)

    def remove(self) -> None:
        if self in sys.meta_path:
            sys.meta_path.remove(self)


def selected_by(node: str, selector: str) -> bool:
    return (
        node == selector
        or node.startswith(selector + "::")
        or node.startswith(selector + "[")
        or node.startswith(selector.rstrip("/") + "/")
    )


def validate_selection(items: list[dict], expected: dict[str, str | None]) -> None:
    for selector, layer in expected.items():
        matched = [item for item in items if selected_by(item["nodeid"], selector)]
        if not matched:
            raise ValueError(f"Mandatory selector resolved no cases: {selector}")
        for item in matched:
            if (layer is not None and item["primary"] != layer) or item["requires_native"]:
                raise ValueError(
                    f"Mandatory selector metadata mismatch: {item['nodeid']}; "
                    f"expected {layer}, no requires_native; observed {item}"
                )


def plan(areas: list[str], extra: list[str]) -> tuple[list[str], dict[str, str | None]]:
    if not areas or any(area not in AREAS for area in areas):
        raise ValueError(f"Select supported behavior areas: {', '.join(AREAS)}")
    expected: dict[str, str | None] = {"tests/unit": "unit", **CORE}
    for area in areas:
        expected.update(AREAS[area])
    roots = list(expected)
    for selector in extra:
        path = Path(selector.split("::")[0])
        if (
            path.is_absolute() or ".." in path.parts or not path.is_file()
            or path.parts[0] != "tests" or not selector.partition("::")[2]
            or not path.resolve().is_relative_to(ROOT / "tests")
        ):
            raise ValueError(f"Additional cases must be exact repo-relative test nodes: {selector}")
        if not any(selected_by(selector, root) for root in roots):
            roots.append(selector)
        # Even a covered root must resolve each explicitly requested regression.
        expected.setdefault(selector, None)
    return list(dict.fromkeys(roots)), expected


def write_json(path: Path, value: object, *, replace: bool = False) -> None:
    path = path.resolve()
    if not path.parent.is_dir():
        raise ValueError(f"Report parent does not exist: {path.parent}")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
