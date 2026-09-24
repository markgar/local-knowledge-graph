# Deliberate schema proposal

Use `kg schema show --json` (or `uv run kg` in a checkout) to inspect exact names,
descriptions, endpoint sets and the active `result.head`. Names and descriptions
are corpus-readable metadata: never copy restricted quotes or identities into them.
Read exact supplied evidence using `kg read document:ID --json`. Copy its support
reference and state without modification. No models are needed for these reads.

Compare reuse, extension and deferral. For a grounded signing certificate that
does not fit the existing types, the following is a template, not executable
evidence. Replace CORPUS, HEAD, ATTRIBUTION, REFERENCE and STATE with real values.
Obtain attribution/corpus from your operator-provided profile. Consider all
plausible current types; the empty candidate list below is not an excuse to skip
discovery. The profile's authority is trusted-local, not a network credential.

```json
{
  "interface_version": "schema-proposal/1",
  "corpus_id": "CORPUS",
  "base_revision": "HEAD",
  "attribution": "ATTRIBUTION",
  "rationale": "Represent supported certificates without calling them projects.",
  "add_entity_types": [{
    "definition": {
      "name": "certificate",
      "description": "An explicitly identified signing or identity certificate."
    },
    "review": {
      "candidates": [],
      "no_existing_candidate_reason": "Replace with the actual vocabulary comparison.",
      "reuse_assessment": "Person/project would distort the supported meaning.",
      "extension_rationale": "The exact example identifies a certificate.",
      "defer_assessment": "Defer if its identity or classification is not supported.",
      "example_ids": ["certificate-example"]
    }
  }],
  "examples": [{
    "example_id": "certificate-example",
    "reference": "REFERENCE",
    "state_version": "STATE",
    "explanation": "The original sentence explicitly mentions the signing certificate."
  }]
}
```

Each change needs 1..10 example IDs and a review. Candidate entries have
`{"term":{"kind":"entity_type","name":"project"},"assessment":"..."}`; kinds also
include `predicate` and `identifier_scheme`. `kg schema validate --schema` is the
complete strict input reference. Added predicates use existing scalar/entity
value kinds. `widen_predicates` takes a name, additional subject/object type lists
and a review; it cannot change meaning, description, value kind or decision encoding.
Validation returns full before/after endpoint sets and counts of newly admitted
Cartesian-product combinations, not newly asserted facts.

Save the prepared proposal. Run `kg schema validate proposal.json --json`.
This checks structure, exact current support, base revision and bounds, NOT semantic
truth. Stop for human review of the exact digest, vocabulary publication and scope.
Only then may the operator run:

```text
kg schema apply proposal.json --approve-digest DIGEST --retry-key SAVED-KEY --approval-rationale "Reviewed exact proposal and publication scope" --json
```

Keep the proposal, key, approval rationale and complete receipt. Unknown outcome:
repeat only that exact prepared request/key. Different payload with the same key
conflicts; a fresh key against an old base is stale. Do not auto-rebase or auto-approve.
Applied schema creates no entities/assertions. Rediscover the new head and put
that exact object in record input's `expected_schema_revision`; separately submit
supported facts. New literal properties are readable facts, not arbitrary graph queries.

Uncertain classifications stay deferred in this release; do not invent an `other`
type or identity merge. Report recorded claims and omissions separately.
