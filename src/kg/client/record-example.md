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
The following creates both entities explicitly:

```json
{
  "expected_schema_revision": "SCHEMA_REVISION",
  "support": {"meeting": "SUPPORT"},
  "changes": [
    {
      "kind": "entity", "local_id": "mira", "name": "Mira", "entity_type": "person",
      "support": {"kind": "source", "evidence": ["meeting"]}
    },
    {
      "kind": "entity", "local_id": "atlas", "name": "Atlas", "entity_type": "project",
      "support": {"kind": "source", "evidence": ["meeting"]}
    },
    {
      "kind": "assertion", "local_id": "ownership",
      "subject": {"kind": "local", "local_id": "mira"}, "predicate": "owns",
      "object": {"kind": "entity", "entity": {"kind": "local", "local_id": "atlas"}},
      "interpretation": "explicit",
      "support": {"kind": "source", "evidence": ["meeting"]}
    },
    {
      "kind": "assertion", "local_id": "release",
      "subject": {"kind": "local", "local_id": "atlas"}, "predicate": "decision",
      "object": {"kind": "string", "value": "Ship Friday"},
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
assertion. `kg record facts.json --json` returns the complete canonical write
receipt, including every local-ID mapping; retain it. Inspect mapped assertions
with `kg read fact:ID`; withdraw only an owned assertion with
`kg remove fact:ID --confirm`. Neither action purges source history.

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
