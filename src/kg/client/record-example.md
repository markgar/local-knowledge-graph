# Grounded record input

Start with `kg record --help` and `kg record --schema`. The schema is also returned
as `result.schema` with `--json`. No profile is needed to read these instructions.

Read a document using `kg read document:ID --json`. Copy one returned
`result.entries[i].support` object, unchanged, into the top-level `support` array.
Use its `reference` in each change's source `evidence` array. For several anchors,
copy each distinct support once. Dependencies are derived from those exact
captured states, never looked up from the latest document. Stale support is rejected.

Use `kg find entities --json` or an exact name/alias search first. For deliberate
reuse, copy a returned entry's `reference` object directly into `subject`, `entity`,
or an entity object's `entity`. For deliberate creation, declare an `entity`
change with a unique local ID, then refer to it with `{"kind":"local","local_id":"..."}`.
Names never implicitly create or select endpoints.

For example, after adding text that actually supports "Mira owns Atlas; Atlas
will ship Friday", substitute the exact returned support for SUPPORT below and
its reference for EVIDENCE. The following creates both entities explicitly:

```json
{
  "support": ["SUPPORT"],
  "changes": [
    {
      "kind": "entity", "local_id": "mira", "name": "Mira", "entity_type": "person",
      "support": {"kind": "source", "evidence": ["EVIDENCE"]}
    },
    {
      "kind": "entity", "local_id": "atlas", "name": "Atlas", "entity_type": "project",
      "support": {"kind": "source", "evidence": ["EVIDENCE"]}
    },
    {
      "kind": "assertion", "local_id": "ownership",
      "subject": {"kind": "local", "local_id": "mira"}, "predicate": "owns",
      "object": {"kind": "entity", "entity": {"kind": "local", "local_id": "atlas"}},
      "interpretation": "explicit",
      "support": {"kind": "source", "evidence": ["EVIDENCE"]}
    },
    {
      "kind": "assertion", "local_id": "release",
      "subject": {"kind": "local", "local_id": "atlas"}, "predicate": "decision",
      "object": {"kind": "string", "value": "Ship Friday"},
      "interpretation": "explicit",
      "support": {"kind": "source", "evidence": ["EVIDENCE"]}
    }
  ]
}
```

SUPPORT and EVIDENCE are object placeholders, not literal strings to submit.
To reuse Atlas in a later submission, omit its creation change and replace its
local reference with the exact `reference` object returned by
`kg find entities Atlas --json`. Keep the actual evidence that supports the new
assertion. `kg record facts.json --json` returns the complete canonical write
receipt, including every local-ID mapping; retain it. Inspect mapped assertions
with `kg read fact:ID`; withdraw only an owned assertion with
`kg remove fact:ID --confirm`. Neither action purges source history.

The native change schema also supports grounded aliases, identifiers, passage
mentions and independent support for existing entities. Every reference must
belong to the configured authorized scope and registered vocabulary. Seed-only
input, extra fields, implicit endpoints and inconsistent captured states are
rejected. File limit: 8,000,000 bytes; up to 100 changes and 200 evidence
occurrences across the changes. Do not label an inference "explicit".
