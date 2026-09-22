"""Hybrid retrieval: metadata filter, then lexical and semantic search fused.

THREE PARTS, IN THIS ORDER, EACH DOING WHAT IT IS GOOD AT.

1. METADATA FILTER, in the WHERE clause. Jurisdiction and effective dates are
   facts, not similarities. A superseded 2024 policy is *about the same subject*
   as the 2026 one that replaced it -- which is exactly what a vector search
   rewards. Filtering first is the difference between citing current policy and
   citing policy that was withdrawn.

2. LEXICAL, via Postgres full-text. Exact terms matter here: "21 days", "GB",
   "points multiplier". Embeddings are lossy about precisely the tokens a policy
   question hinges on.

3. SEMANTIC, via pgvector. Catches the questions that share no vocabulary with
   the answer -- "why did people stop coming back" against a document that says
   "lapse-triggered contact".

FUSION IS RECIPROCAL RANK FUSION. The two searches return incomparable scores:
ts_rank is unbounded and corpus-dependent, cosine distance is [0,2]. Normalising
them against each other requires a calibration that would need re-measuring
whenever the corpus changes. RRF uses only the RANKS, so it needs no calibration
and cannot be skewed by one search's score distribution. A document found by
both rises above one found strongly by either.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from psycopg.rows import dict_row

from . import embeddings

# The usual constant. Large enough that the top few results are not allowed to
# dominate purely by being first in one of the two lists.
RRF_K = 60
CANDIDATES = 30          # taken from each search before fusion


@dataclass(frozen=True)
class Passage:
    chunk_id: int
    slug: str
    title: str
    doc_type: str
    jurisdiction: str
    heading: str | None
    text: str
    effective_from: date
    effective_to: date | None
    lexical_rank: int | None
    semantic_rank: int | None
    score: float

    def cite(self) -> str:
        return f"{self.slug}#{self.heading}" if self.heading else self.slug


FILTERS = """
    (%(jurisdictions)s::text[] is null
         or d.jurisdiction = any(%(jurisdictions)s))
    and d.effective_from <= %(as_of)s
    and (d.effective_to is null or d.effective_to > %(as_of)s)
    and (%(doc_types)s::text[] is null
         or d.doc_type = any(%(doc_types)s))
"""

LEXICAL_SQL = f"""
    select c.chunk_id, ts_rank(c.tsv, q) as rank
    from knowledge.chunks c
    join knowledge.documents d on d.document_id = c.document_id,
         websearch_to_tsquery('english', %(lexical_query)s) q
    where c.tsv @@ q and {FILTERS}
    order by rank desc limit {CANDIDATES}
"""

SEMANTIC_SQL = f"""
    select c.chunk_id, c.embedding <=> %(vector)s::vector as distance
    from knowledge.chunks c
    join knowledge.documents d on d.document_id = c.document_id
    where c.embedding is not null and {FILTERS}
    order by distance asc limit {CANDIDATES}
"""

HYDRATE_SQL = """
    select c.chunk_id, c.heading, c.text, d.slug, d.title, d.doc_type,
           d.jurisdiction, d.effective_from, d.effective_to
    from knowledge.chunks c
    join knowledge.documents d on d.document_id = c.document_id
    where c.chunk_id = any(%s)
"""


def _lexical_query(query: str) -> str:
    """Turn a natural question into an OR query.

    websearch_to_tsquery ANDs every term by default, so a seven-word question
    matches only a chunk containing all seven -- which is essentially never.
    Measured on the first spot check: lexical search contributed nothing to two
    of three queries, leaving "hybrid" retrieval that was semantic-only.

    ORing the terms and letting ts_rank do the work restores what lexical search
    is for: a chunk containing "21 days" and "lapse" outranks one containing
    neither, and exact tokens -- dates, codes, amounts -- are matched exactly
    rather than approximately.
    """
    terms = [t for t in "".join(
        ch if ch.isalnum() else " " for ch in query).split() if len(t) > 2]
    return " or ".join(terms) if terms else query


def search(conn, query: str, *, jurisdiction: str | None = None,
           doc_types: list[str] | None = None, as_of: date | None = None,
           limit: int = 6) -> list[Passage]:
    """Return the best passages for a query.

    `jurisdiction=None` means NO jurisdiction filter, not "global only". An
    earlier version defaulted to GLOBAL alone, which made every market-specific
    document unreachable unless the caller already knew which market to ask
    about -- and a question that mentions Britain in its text does not pass a
    jurisdiction. The retrieval eval caught it: all five GB/IN/AE questions
    missed, which is most of the gap between 0.73 and 0.90 recall@3.

    Passing a jurisdiction narrows to that market PLUS global, because a
    market-specific question is still governed by global policy.
    """
    params = {
        "query": query,
        "lexical_query": _lexical_query(query),
        "jurisdictions": ["GLOBAL", jurisdiction] if jurisdiction else None,
        "doc_types": doc_types or None,
        # Defaults to today, so withdrawn policy is excluded unless a caller
        # deliberately asks what was in force at some past date.
        "as_of": as_of or date.today(),
    }

    with conn.cursor(row_factory=dict_row) as cur:
        lexical = [r["chunk_id"] for r in cur.execute(LEXICAL_SQL, params).fetchall()]

        params["vector"] = str(embeddings.create().embed_query(query))
        semantic = [r["chunk_id"] for r in cur.execute(SEMANTIC_SQL, params).fetchall()]

        lexical_rank = {cid: i + 1 for i, cid in enumerate(lexical)}
        semantic_rank = {cid: i + 1 for i, cid in enumerate(semantic)}
        scores = {
            cid: (1 / (RRF_K + lexical_rank.get(cid, 10**6))
                  + 1 / (RRF_K + semantic_rank.get(cid, 10**6)))
            for cid in set(lexical) | set(semantic)
        }
        ordered = sorted(scores, key=lambda cid: -scores[cid])
        if not ordered:
            return []

        # Hydrate more than we need so the per-document cap below has something
        # to fall back on.
        rows = {r["chunk_id"]: r for r in
                cur.execute(HYDRATE_SQL, (ordered[:limit * 3],)).fetchall()}

    # At most two chunks from any one document. Without this a single long
    # policy can fill every slot with adjacent sections, and the caller sees one
    # source where it asked for several. Two rather than one because a policy
    # genuinely can answer a question across two sections.
    best: list[int] = []
    per_document: dict[str, int] = {}
    for cid in ordered:
        row = rows.get(cid)
        if row is None:
            continue
        if per_document.get(row["slug"], 0) >= 2:
            continue
        per_document[row["slug"]] = per_document.get(row["slug"], 0) + 1
        best.append(cid)
        if len(best) == limit:
            break

    return [Passage(chunk_id=cid, slug=rows[cid]["slug"], title=rows[cid]["title"],
                    doc_type=rows[cid]["doc_type"],
                    jurisdiction=rows[cid]["jurisdiction"],
                    heading=rows[cid]["heading"], text=rows[cid]["text"],
                    effective_from=rows[cid]["effective_from"],
                    effective_to=rows[cid]["effective_to"],
                    lexical_rank=lexical_rank.get(cid),
                    semantic_rank=semantic_rank.get(cid),
                    score=scores[cid])
            for cid in best]
