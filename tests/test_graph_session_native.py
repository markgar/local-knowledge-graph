
import pytest
from support.graph import fixture, require_native

from kg.evidence._read_context import read_evidence
from kg.graph import LocalGraphSession


@pytest.mark.native
@pytest.mark.requires_native
def test_real_native_lifecycle_sourceproofs_and_restart(tmp_path):
    require_native()
    env = fixture(tmp_path / "source.sqlite", decisions=12)
    def consume(context):
        rows = context.native.execute("MATCH (a:Assertion) RETURN count(a)", {})
        result = rows.read()
        rows.close()
        evidence = read_evidence(context.canonical, env.references[0])
        return context.retain((result, evidence))
    with LocalGraphSession(env.database, env.identity, env.scope,
                           graph_directory=tmp_path / "derived") as session:
        first = session._run_read(env.scope, consume)
        generation = session._generation
        assert first[0] == ((22,),)
        assert first[1].reference == env.references[0]
        assert session._run_read(env.scope, consume) == first
        assert session._generation == generation
        assert session.refresh().state == "ready"
        assert session._generation != generation
        assert session._run_read(env.scope, consume) == first
    with LocalGraphSession(env.database, env.identity, env.scope,
                           graph_directory=tmp_path / "derived") as restarted:
        assert restarted.status().state == "unbuilt"
        assert restarted._run_read(env.scope, consume) == first
        assert restarted._generation.session_id != generation.session_id
