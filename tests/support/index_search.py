"""Controlled canonical search providers, never model-quality evidence."""

from kg.indexing import EvidenceSearchService
from kg.retrieval.rerank import MODEL_NAME, MODEL_REVISION, SCORING_PIPELINE_VERSION
from support.evidence import receipt
from support.indexing import ControlledProvider, process, request, service


class SearchProvider(ControlledProvider):
    def encode_query(self, text):
        if self.before_encode:
            self.before_encode()
        self.calls.append(("query", text))
        return [float(len(text) + 1), 1.0] + [0.0] * (self.dimensions - 2)


class Reranker:
    name = MODEL_NAME
    revision = MODEL_REVISION
    pipeline_version = SCORING_PIPELINE_VERSION + "|controlled-test"
    license = "Apache-2.0"

    def __init__(self):
        self.calls = []
        self.before_score = None

    def score(self, query, passages, *, batch_size):
        self.calls.append((query, tuple(passages), batch_size))
        if self.before_score:
            self.before_score()
        return [float(text.casefold().count(query.casefold())) for text in passages]


def prepared(env, **kwargs):
    provider = SearchProvider()
    index, _, loads = service(env, provider)
    value = request(env, **kwargs)
    saved = receipt(env.service.write(value))
    assert process(index, env, value, saved).outcome == "ready"
    search = EvidenceSearchService(env.database, env.service.identity)
    search._provider_factory = lambda profile: provider
    reranker = Reranker()
    search._reranker_factory = lambda: reranker
    return search, index, provider, reranker, value, saved


def blocked_search(path, ready, resume, phase):
    from kg.evidence._sql import AccountedConnection
    from support.evidence import environment

    env = environment(path)
    search = EvidenceSearchService(env.database, env.service.identity)
    search._provider_factory = lambda _: SearchProvider()
    reranker = Reranker()
    search._reranker_factory = lambda: reranker

    def block():
        ready.set()
        if not resume.wait(30):
            raise AssertionError("Parent failed to terminate or release search")

    if phase == "rerank":
        reranker.before_score = block
    else:
        original = AccountedConnection.execute

        def insert(connection, sql, parameters=()):
            result = original(connection, sql, parameters)
            if sql.startswith("INSERT INTO temp.e3_search"):
                block()
            return result

        AccountedConnection.execute = insert
    search.search(env.scope, "alpha")
