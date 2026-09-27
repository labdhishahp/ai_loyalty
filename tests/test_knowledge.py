"""What retrieval must get right, and what it must refuse to do.

NO NETWORK, EVER. Every test here either exercises a pure function or replaces
the embedder with a stub. Real embedding calls would make the suite depend on a
third party's uptime and make failures ambiguous -- a red test would mean
"retrieval is broken" or "Hugging Face is slow" with no way to tell which.

The stub returns a REAL stored vector rather than random numbers. That matters:
a random query vector makes semantic ranking arbitrary, so a test written
against it asserts nothing. Embedding a chunk that is already in the index gives
that chunk a cosine distance of exactly zero, which makes the semantic half of
the search deterministic and therefore testable.

Retrieval QUALITY is not tested here. Recall@3 is measured against a gold set by
eval/run_retrieval.py, because quality is a number to track, not a boolean to
assert. What is tested here is the behaviour that must hold regardless of
quality: the filters, the fusion arithmetic, and the failure cases.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from knowledge import embeddings, ingest, retrieval

# The corpus's one withdrawn document: superseded on 2026-02-01 by the policy
# that replaced it. Retrieval exists partly to never cite this.
SUPERSEDED = "points-validity-policy-2024"


# ---------------------------------------------------------------------------
# Pure functions. No database, no network.
# ---------------------------------------------------------------------------

def test_lexical_query_ors_terms_rather_than_anding_them():
    """The regression that made "hybrid" retrieval semantic-only.

    websearch_to_tsquery ANDs by default, so a seven-word question matched only
    a chunk containing all seven words -- which no chunk does. Lexical search
    contributed nothing to two of three spot-checked queries before this.
    """
    assert retrieval._lexical_query("why did gold members stop ordering") == (
        "why or did or gold or members or stop or ordering")


def test_lexical_query_drops_noise_but_keeps_digits():
    """Tokens of two characters or fewer are noise. Numbers are not: "21 days"
    and "90 days" are the entire difference between two policies."""
    built = retrieval._lexical_query("do 21 day points expire in GB?")
    assert "day" in built and "points" in built and "expire" in built
    assert " in " not in f" {built} "          # dropped: two characters
    assert "gb" not in built.split(" or ")     # dropped: two characters


def test_lexical_query_falls_back_when_everything_is_noise():
    """An all-short-token query must not produce an empty tsquery, which
    websearch_to_tsquery would reject."""
    assert retrieval._lexical_query("is it on?") == "is it on?"


def test_chunk_splits_on_headings_and_keeps_the_lead():
    body = ("Intro text before any heading.\n\n"
            "## Discount ceilings\n" + "A" * 100 + "\n\n"
            "## Consent\n" + "B" * 100)
    chunks = ingest.chunk(body)
    assert [heading for heading, _ in chunks] == [
        None, "Discount ceilings", "Consent"]
    assert chunks[0][1] == "Intro text before any heading."


def test_chunk_merges_a_section_too_short_to_retrieve():
    """A two-word section retrieves noisily and tells the reader nothing, so it
    joins the section above it rather than becoming a chunk of its own."""
    body = "## Ceilings\n" + "A" * 100 + "\n\n## Note\nSee above."
    chunks = ingest.chunk(body)
    assert len(chunks) == 1
    assert chunks[0][0] == "Ceilings"
    assert "## Note" in chunks[0][1] and "See above." in chunks[0][1]


def test_chunk_handles_a_document_with_no_headings():
    assert ingest.chunk("Just a paragraph.") == [(None, "Just a paragraph.")]


def test_parse_refuses_a_document_without_front_matter(tmp_path):
    """Front matter carries jurisdiction and effective dates. A document
    without them cannot be filtered, so it must not be silently ingested with
    defaults that would make withdrawn policy look current."""
    path = tmp_path / "broken.md"
    path.write_text("## Heading\nNo front matter here.")
    with pytest.raises(ValueError, match="front matter"):
        ingest.parse(path)


def test_unit_scales_to_length_one():
    scaled = embeddings._unit([3.0, 4.0])
    assert scaled == pytest.approx([0.6, 0.8])
    assert sum(v * v for v in scaled) == pytest.approx(1.0)


def test_unit_leaves_a_zero_vector_alone_rather_than_dividing_by_zero():
    assert embeddings._unit([0.0, 0.0]) == [0.0, 0.0]


def test_create_refuses_an_unlisted_model(monkeypatch):
    """The column is vector(384). A model of the wrong width fails at insert
    time with an error that says nothing about the cause, so it is refused
    here, where the message can name the fix."""
    monkeypatch.setenv("HF_TOKEN", "not-a-real-token")
    with pytest.raises(embeddings.EmbeddingError) as exc:
        embeddings.create("some-org/some-unlisted-model")
    assert "KNOWN_MODELS" in str(exc.value)


def test_create_derives_dimension_from_the_model(monkeypatch):
    """Dimension is never configured separately: two settings that must agree
    are two settings that will eventually disagree."""
    monkeypatch.setenv("HF_TOKEN", "not-a-real-token")
    monkeypatch.setenv("EMBEDDING_MODEL", "BAAI/bge-base-en-v1.5")
    assert embeddings.create().dimension == 768


# ---------------------------------------------------------------------------
# Retrieval against the real index, with the embedder stubbed.
# ---------------------------------------------------------------------------

@pytest.fixture
def indexed_chunk(db_conn):
    """A chunk that is in the index, with its stored vector.

    Used as the stub's query embedding so that this chunk is at distance zero
    and therefore first in the semantic list, deterministically.
    """
    row = db_conn.execute("""
        select c.chunk_id, c.embedding::text, d.slug
        from knowledge.chunks c
        join knowledge.documents d on d.document_id = c.document_id
        where c.embedding is not null and d.effective_to is null
        order by c.chunk_id limit 1
    """).fetchone()
    if row is None:
        pytest.skip("knowledge corpus is not ingested")
    return {"chunk_id": row[0], "vector": row[1], "slug": row[2]}


@pytest.fixture
def stub_embedder(monkeypatch, indexed_chunk):
    """Replace the embedder with one that returns a known indexed vector."""
    class Stub:
        dimension = 384
        def embed_query(self, text):
            return [float(v) for v in indexed_chunk["vector"].strip("[]").split(",")]

    monkeypatch.setattr(embeddings, "create", lambda model=None: Stub())
    return indexed_chunk


def test_search_finds_the_chunk_whose_vector_was_the_query(db_conn, stub_embedder):
    """End to end: filter, both searches, fusion, hydration, cap.

    A chunk embedded by its own vector has distance zero, so it must come back
    ranked first semantically. If hydration or the per-document cap dropped it,
    this is the test that notices.
    """
    passages = retrieval.search(db_conn, "points expiry rules", limit=6)
    found = [p for p in passages if p.chunk_id == stub_embedder["chunk_id"]]
    assert found, "the chunk at distance zero was not returned"
    assert found[0].semantic_rank == 1


def test_search_excludes_a_superseded_document(db_conn, stub_embedder):
    """The reason the metadata filter runs before the vector search.

    The withdrawn 2024 policy is ABOUT the same subject as the 2026 one that
    replaced it, which is exactly what semantic similarity rewards. Citing it
    would be worse than returning nothing.
    """
    passages = retrieval.search(
        db_conn, "how long do points stay valid before they expire", limit=20)
    assert SUPERSEDED not in {p.slug for p in passages}


def test_search_can_still_reach_withdrawn_policy_as_of_a_past_date(db_conn,
                                                                   stub_embedder):
    """Excluded by default, not deleted. "What was in force in June 2025" is a
    real question, and answering it is the point of storing effective dates
    rather than dropping superseded rows."""
    passages = retrieval.search(
        db_conn, "how long do points stay valid before they expire",
        as_of=date(2025, 6, 1), limit=20)
    assert SUPERSEDED in {p.slug for p in passages}


def test_search_without_a_jurisdiction_does_not_mean_global_only(db_conn,
                                                                 stub_embedder):
    """The bug that cost most of the gap between 0.73 and 0.95 recall@3.

    An earlier version defaulted to GLOBAL, which made every market-specific
    document unreachable unless the caller already knew the market -- and a
    question that mentions Britain in its text does not pass a jurisdiction.
    """
    passages = retrieval.search(
        db_conn, "beauty supply incident in Britain stock shortage",
        jurisdiction=None, limit=20)
    assert {p.jurisdiction for p in passages} != {"GLOBAL"}


def test_search_with_a_jurisdiction_keeps_global_and_excludes_other_markets(
        db_conn, stub_embedder):
    """A GB question is still governed by global policy, so GB means GB plus
    GLOBAL -- but never IN or AE."""
    passages = retrieval.search(db_conn, "campaign rules and discount ceilings",
                                jurisdiction="GB", limit=20)
    assert passages
    assert {p.jurisdiction for p in passages} <= {"GB", "GLOBAL"}


def test_search_respects_a_document_type_filter(db_conn, stub_embedder):
    passages = retrieval.search(db_conn, "what happened and why",
                                doc_types=["postmortem"], limit=20)
    assert passages
    assert {p.doc_type for p in passages} == {"postmortem"}


def test_search_caps_chunks_from_any_one_document(db_conn, stub_embedder):
    """Without the cap a single long policy fills every slot with adjacent
    sections, and the caller sees one source where it asked for several. The
    corpus has documents with seven chunks, so this is reachable."""
    passages = retrieval.search(db_conn, "campaign governance policy rules "
                                         "approval holdout consent ceilings",
                                limit=6)
    counts: dict[str, int] = {}
    for passage in passages:
        counts[passage.slug] = counts.get(passage.slug, 0) + 1
    assert max(counts.values()) <= 2


def test_search_honours_its_limit(db_conn, stub_embedder):
    assert len(retrieval.search(db_conn, "loyalty points policy", limit=3)) <= 3


def test_search_scores_are_reciprocal_rank_fusion_and_sorted(db_conn,
                                                             stub_embedder):
    """Fusion uses only ranks, never the two searches' incomparable scores.

    Recomputing the score from each passage's own ranks is what catches a
    future edit that starts blending ts_rank with cosine distance -- which
    would look fine until the corpus changed and the calibration went stale.
    """
    passages = retrieval.search(db_conn, "points expiry and campaign rules",
                                limit=6)
    assert passages
    missing = 10 ** 6
    for passage in passages:
        expected = (1 / (retrieval.RRF_K + (passage.lexical_rank or missing))
                    + 1 / (retrieval.RRF_K + (passage.semantic_rank or missing)))
        assert passage.score == pytest.approx(expected)
    assert [p.score for p in passages] == sorted(
        (p.score for p in passages), reverse=True)


def test_search_returns_nothing_rather_than_failing_when_filters_exclude_all(
        db_conn, stub_embedder):
    """An empty result is an answer. The caller must get [] rather than an
    exception, because "no current policy covers this" is a legitimate finding
    the agent has to be able to report."""
    assert retrieval.search(db_conn, "points expiry",
                            as_of=date(1999, 1, 1), limit=6) == []


def test_passage_cite_names_the_section_when_there_is_one():
    """Citations are checked by a human against the corpus, so they must point
    at a section rather than a whole policy."""
    def passage(heading):
        return retrieval.Passage(
            chunk_id=1, slug="campaign-governance-policy", title="T",
            doc_type="policy", jurisdiction="GLOBAL", heading=heading,
            text="...", effective_from=date(2026, 1, 1), effective_to=None,
            lexical_rank=1, semantic_rank=None, score=0.0)

    assert passage("Holdouts").cite() == "campaign-governance-policy#Holdouts"
    assert passage(None).cite() == "campaign-governance-policy"


# ---------------------------------------------------------------------------
# The retry budget. No network: the opener is replaced.
# ---------------------------------------------------------------------------

def test_the_retry_policy_is_bounded_by_a_deadline(monkeypatch):
    """The whole point of the change, measured rather than asserted.

    The old policy was three attempts at 60s with a 5s/10s backoff -- a worst
    case of 195 seconds inside a function Vercel kills at 60. Here the clock is
    fake and every attempt "hangs", so a policy that ignored the deadline would
    record attempts until it ran out of tries.
    """
    import urllib.error

    monkeypatch.setenv("EMBEDDING_BUDGET_SECONDS", "30")
    monkeypatch.setenv("EMBEDDING_TIMEOUT_SECONDS", "15")

    now = {"t": 0.0}
    attempts: list[float] = []
    monkeypatch.setattr(embeddings.time, "monotonic", lambda: now["t"])
    monkeypatch.setattr(embeddings.time, "sleep",
                        lambda seconds: now.__setitem__("t", now["t"] + seconds))

    def hang(request, timeout=None):
        attempts.append(timeout)
        now["t"] += timeout                      # the attempt uses its timeout
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr(embeddings.urllib.request, "urlopen", hang)

    embedder = embeddings.Embedder(model="BAAI/bge-small-en-v1.5",
                                   dimension=384, token="t")
    with pytest.raises(embeddings.EmbeddingError):
        embedder.embed_query("anything")

    assert sum(attempts) + 0 <= 30, f"spent {sum(attempts)}s of a 30s budget"
    assert now["t"] <= 30, f"wall clock reached {now['t']}s against a 30s budget"


def test_an_attempt_never_outlives_the_remaining_budget(monkeypatch):
    """A late attempt is capped at what is left, not at the full per-attempt
    timeout -- otherwise the last try alone could overrun the budget."""
    import urllib.error

    monkeypatch.setenv("EMBEDDING_BUDGET_SECONDS", "20")
    monkeypatch.setenv("EMBEDDING_TIMEOUT_SECONDS", "15")

    now = {"t": 0.0}
    attempts: list[float] = []
    monkeypatch.setattr(embeddings.time, "monotonic", lambda: now["t"])
    monkeypatch.setattr(embeddings.time, "sleep",
                        lambda seconds: now.__setitem__("t", now["t"] + seconds))

    def hang(request, timeout=None):
        attempts.append(timeout)
        now["t"] += timeout
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr(embeddings.urllib.request, "urlopen", hang)
    embedder = embeddings.Embedder(model="BAAI/bge-small-en-v1.5",
                                   dimension=384, token="t")
    with pytest.raises(embeddings.EmbeddingError):
        embedder.embed_query("anything")

    assert attempts[0] == 15
    assert all(a <= 15 for a in attempts)
    assert now["t"] <= 20


def test_a_cold_start_is_still_retried_when_there_is_time(monkeypatch):
    """503 means the model is loading, not that the request failed. Bounding
    the budget must not turn a recoverable cold start into a hard failure."""
    import io
    import urllib.error

    monkeypatch.setenv("EMBEDDING_BUDGET_SECONDS", "120")
    monkeypatch.setenv("EMBEDDING_TIMEOUT_SECONDS", "15")
    monkeypatch.setattr(embeddings.time, "sleep", lambda seconds: None)

    calls = {"n": 0}

    class Response:
        def read(self):
            return json.dumps([[0.0] * 383 + [1.0]]).encode()
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False

    def flaky(request, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(
                "u", 503, "loading", {}, io.BytesIO(b"loading"))
        return Response()

    monkeypatch.setattr(embeddings.urllib.request, "urlopen", flaky)
    embedder = embeddings.Embedder(model="BAAI/bge-small-en-v1.5",
                                   dimension=384, token="t")
    vector = embedder.embed_query("anything")

    assert calls["n"] == 2, "the cold start was not retried"
    assert len(vector) == 384
