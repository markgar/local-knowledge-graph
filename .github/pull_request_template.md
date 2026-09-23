## Summary

Describe the problem and the approach taken.

## Verification

List the tests and checks run.

## Checklist

- [ ] Behavior changes include tests.
- [ ] Public contracts and documentation are updated.
- [ ] Schema changes update the owning schema: `src/kg/evidence/schema.sql` for canonical evidence, `src/kg/schema.sql` only for the Markdown demonstration. Exact evidence/history remain intact within supported stores; a format change may require an explicit fresh store/reload, not migration or backward compatibility. Incompatible stores are never silently altered/deleted; graph/vector projections remain disposable.
- [ ] No corpus-specific behavior was added to production code.
