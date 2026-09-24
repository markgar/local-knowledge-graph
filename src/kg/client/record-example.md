# Grounded record input

Start with `kg record --help` and `kg record --schema`. The schema is also returned
as `result.schema` with `--json`. No profile is needed to read these instructions.

Read a document using `kg read document:ID --json`. Copy one returned
`result.entries[i].support` object, unchanged, into the top-level `support` map
under a request-local name such as `meeting`. Use `["meeting"]` in each change's
source `evidence` array. For several anchors, declare each distinct support once
and list all needed names: `["meeting", "appendix"]` means both are required.
Dependencies are derived from those exact
captured states, never looked up from the latest document. Stale support is rejected.

Inspect `kg schema show --json` first. Copy its exact `result.revision` object
into `expected_schema_revision`. Fresh submissions require that exact head;
if it changes, review the vocabulary and prepare a new submission rather than
silently replacing the precondition. Historical contributions retain their
original authoring revision.

Use `kg find entities --json` or an exact name/alias search first. For deliberate
reuse, copy a returned entry's `reference` object directly into `subject`, `entity`,
or an entity object's `entity`. For deliberate creation, declare an `entity`
change with a unique local ID, then refer to it with `{"kind":"local","local_id":"..."}`.
Names never implicitly create or select endpoints.

For example, after adding text that actually supports "Mira owns Atlas; Atlas
will ship Friday", substitute the exact returned support object for SUPPORT below.
The following creates both identities, authors their classifications, explicitly
selects each claim, and captures those selections in the assertions:

```json
{
  "expected_schema_revision": "SCHEMA_REVISION",
  "support": {"meeting": "SUPPORT"},
  "changes": [
    {
      "kind": "entity", "local_id": "mira", "name": "Mira",
      "support": {"kind": "source", "evidence": ["meeting"]}
    },
    {
      "kind": "entity", "local_id": "atlas", "name": "Atlas",
      "support": {"kind": "source", "evidence": ["meeting"]}
    },
    {
      "kind": "classification", "local_id": "mira-type",
      "entity": {"kind": "local", "local_id": "mira"}, "entity_type": "person",
      "interpretation": "explicit", "support": {"kind": "source", "evidence": ["meeting"]}
    },
    {
      "kind": "classification", "local_id": "atlas-type",
      "entity": {"kind": "local", "local_id": "atlas"}, "entity_type": "project",
      "interpretation": "explicit", "support": {"kind": "source", "evidence": ["meeting"]}
    },
    {
      "kind": "classification_selection", "local_id": "mira-selection",
      "entity": {"kind": "local", "local_id": "mira"},
      "claim": {"kind": "local", "local_id": "mira-type"},
      "expected_selection_id": null, "reviewed_candidates_digest": null,
      "reviewed_claim_ids": [], "review_coverage": "complete", "accept_incomplete_review": false,
      "rationale": "Selected the explicitly supported person claim."
    },
    {
      "kind": "classification_selection", "local_id": "atlas-selection",
      "entity": {"kind": "local", "local_id": "atlas"},
      "claim": {"kind": "local", "local_id": "atlas-type"},
      "expected_selection_id": null, "reviewed_candidates_digest": null,
      "reviewed_claim_ids": [], "review_coverage": "complete", "accept_incomplete_review": false,
      "rationale": "Selected the explicitly supported project claim."
    },
    {
      "kind": "assertion", "local_id": "ownership",
      "subject": {"kind": "local", "local_id": "mira"}, "predicate": "owns",
      "object": {"kind": "entity", "entity": {"kind": "local", "local_id": "atlas"}},
      "subject_classification": {"kind": "local", "local_id": "mira-selection"},
      "object_classification": {"kind": "local", "local_id": "atlas-selection"},
      "interpretation": "explicit",
      "support": {"kind": "source", "evidence": ["meeting"]}
    },
    {
      "kind": "assertion", "local_id": "release",
      "subject": {"kind": "local", "local_id": "atlas"}, "predicate": "decision",
      "object": {"kind": "string", "value": "Ship Friday"},
      "subject_classification": {"kind": "local", "local_id": "atlas-selection"},
      "interpretation": "explicit",
      "support": {"kind": "source", "evidence": ["meeting"]}
    }
  ]
}
```

SUPPORT and SCHEMA_REVISION are object placeholders, not literal strings to submit.
To reuse Atlas in a later submission, omit its creation change and replace its
local reference with the exact `reference` object returned by
`kg find entities Atlas --json`. Keep the actual evidence that supports the new
assertion. Read its current `classification.selection_id` and use
`{"kind":"stored","event_id":"RETURNED_SELECTION_ID"}` for
`subject_classification` (and `object_classification` for entity objects).
Do not automatically replace stale preconditions.
`kg record facts.json --retry-key reviewed-facts-1 --json` returns the complete canonical write
receipt, including every local-ID mapping; retain it. Inspect mapped assertions
with `kg read fact:ID`; withdraw only an owned assertion with
`kg remove fact:ID --confirm`. Neither action purges source history.

An identifiable thing can instead remain unresolved: submit only an `entity`
change with exact existence support. No type or edge is required. Do not coerce
"the export" into project/other, and do not equate unrelated document-local mentions
by name. `entity_type` is not accepted on identity creation or independent support.
Classification claims never supply identity support.

For refinement of an existing entity, add a supported `classification` claim, then
run `kg classifications entity:ID --json`. The `result` contains `selection_id`,
`reviewed_candidates_digest`, `reviewed_claim_ids` and `review_coverage`. Copy these
unchanged into a `classification_selection` change, with `entity` the stored
reference, `claim: {"kind":"stored","contribution_id":"CLAIM_ID"}`, and an explicit
`rationale`. Selection-only input uses `"support":[]`; the change has no support
field. Only the original identity owner/writer can select or clear (`"claim":null`).
Claims are authors' supported interpretations, not verified truth; newest never wins.

Default review is complete for authorized current claims or fails at 200 claims/
the ordinary budget. `--review-claim fact:ID` (repeatable), or `--review-empty` for
clearing, deliberately requests a bounded subset. It requires
`review_coverage:"selected_subset"` and `accept_incomplete_review:true` when selecting.
Hidden alternatives are not evidence of agreement. Selection history is available
with `--history`, `--limit`, and opaque `--after-event-id`; inaccessible events,
rationales and cursor gaps are not disclosed.

Persist the exact input and retry key before writing; retry unknown outcomes without
changing either. Withdrawal uses `kg withdraw-classification fact:ID --retry-key KEY`.
It preserves identity/history but permanently disables that claim. Any changed
selection, including same-type new support or A-to-B-to-A, permanently makes old
typed assertions ineligible. Submit a newly reviewed assertion with fresh captures;
refresh/replay does not re-pin it. Original `/4` stores are refused intact: explicitly
initialize a fresh `/5` path and resupply, never silently migrate or delete.

Reference roles are distinct: copy `entry.reference` (the whole JSON object)
into record's `subject` or `entity`; copy `entry.target` (a string such as
`entity:ID` or `fact:ID`) as the argument to `kg read` or an appropriate command.
Copy returned `evidence_targets` to `kg read` to recover exact captured support.
Document targets returned by add/search/read work with document read/update/remove;
updates/removal also need the returned exact state via `--expect`.

The original native form remains accepted: top-level `"support": [SUPPORT]`,
with each change using `"support": {"kind": "source", "evidence": [SUPPORT.reference]}`.
In that notation substitute whole objects, not the literal text `SUPPORT.reference`.
Do not mix native references and names in the named-map form, or names into
the native-array form. Names are nonblank strings of at most 256 characters.
Every declaration must be used. Unknown/duplicate names, duplicate evidence
(including the same evidence under different names), and conflicting captured
states fail before any write. Names exist only within this request.

The native change schema also supports grounded aliases, identifiers, passage
mentions and independent support for existing entities. Every reference must
belong to the configured authorized scope and registered vocabulary. Seed-only
input, extra fields, implicit endpoints and inconsistent captured states are
rejected. File limit: 8,000,000 bytes; up to 100 changes and 200 evidence
occurrences across the changes, counted after shorthand expansion. The complete
expanded canonical write request must also fit its 8,000,000-byte limit.
The schema describes input structure; resolution, unused declarations, exact
state consistency and aggregate limits are additionally checked at runtime.
Do not label an inference "explicit". Empty/incomplete entity matches do not prove
an entity is new: list/page eligible entities or inspect source evidence first.
