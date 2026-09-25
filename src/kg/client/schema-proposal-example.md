# Validate a custom schema proposal

Start from `result.editable_proposal` returned by `kg schema generate`. It already
contains exact corpus, attribution, base, and initial sample bookkeeping. Fill
semantic fields deliberately; do not silently choose identities, names, types,
predicates, interpretations, rationales, or approval.

For an additive configured-corpus proposal, inspect `kg schema show --json` and
copy its exact `result.head`. This minimal example proposes an unrelated custom
vocabulary:

```json
{
  "interface_version": "schema-proposal/1",
  "corpus_id": "COPY-CORPUS",
  "base_revision": "COPY-HEAD-OBJECT",
  "attribution": "COPY-ATTRIBUTION-OBJECT",
  "rationale": "Represent supported hardware structure.",
  "add_entity_types": [{
    "definition": {
      "name": "device",
      "description": "An explicitly identified physical device."
    },
    "review": {
      "candidates": [],
      "no_existing_candidate_reason": "No current term preserves this meaning.",
      "reuse_assessment": "Reviewed the current vocabulary.",
      "extension_rationale": "The exact example identifies a device.",
      "defer_assessment": "Defer if identity or classification is unsupported.",
      "example_ids": ["device-example"]
    }
  }],
  "add_identifier_schemes": [{
    "definition": {
      "name": "asset_tag",
      "description": "An explicitly supplied maintenance asset tag."
    },
    "review": {
      "candidates": [],
      "no_existing_candidate_reason": "No current identifier scheme preserves this meaning.",
      "reuse_assessment": "Reviewed the current vocabulary.",
      "extension_rationale": "The exact example supplies an asset tag.",
      "defer_assessment": "Defer if the tag is not explicit.",
      "example_ids": ["device-example"]
    }
  }],
  "add_predicates": [{
    "definition": {
      "name": "maintenance_note",
      "description": "An explicitly recorded maintenance note.",
      "subject_types": ["device"],
      "object_kind": "string"
    },
    "review": {
      "candidates": [],
      "no_existing_candidate_reason": "No current predicate preserves this meaning.",
      "reuse_assessment": "Reviewed the current vocabulary.",
      "extension_rationale": "The exact example supplies a maintenance note.",
      "defer_assessment": "Defer if the note is not explicit.",
      "example_ids": ["device-example"]
    }
  }],
  "examples": [{
    "example_id": "device-example",
    "reference": "COPY-EXACT-REFERENCE-OBJECT",
    "state_version": "COPY-EXACT-STATE",
    "explanation": "The exact excerpt identifies the device concept."
  }]
}
```

Candidate reviews record reuse, extension, or deferral. Initial generation also
retains the complete returned sample under `initial_generation`; do not shorten it.

Run `kg schema validate proposal.json --json`. Validation checks structure,
support, bounds, and the exact base; it does not establish semantic truth or
approval. Review the returned digest and stale-base behavior before the separate
operator command:

```text
kg schema apply proposal.json --approve-digest DIGEST --retry-key SAVED-KEY \
  --approval-rationale "Reviewed exact proposal and publication scope" --json
```

Do not auto-rebase or auto-approve. Use `kg schema validate --schema` for
predicates, identifier schemes, widening, unresolved concepts, and all strict
fields. Schema apply creates no entities or assertions.
