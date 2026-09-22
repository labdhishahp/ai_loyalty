-- 0004_knowledge_schema.sql
--
-- L-Mart's written knowledge: policies, playbooks, guidelines, post-mortems.
--
-- WHY POLICIES ARE DUAL-NATURE. A policy has to be readable by a person and
-- enforceable by code. The prose body is what the agent cites when explaining;
-- the structured columns are what Milestone 3's validator checks against. One
-- table, two consumers, so a rule cannot be explained one way and enforced
-- another.
--
-- WHY METADATA COLUMNS AND NOT JUST EMBEDDINGS. Semantic similarity alone will
-- cheerfully return a superseded 2024 EU policy for a 2026 UK question -- they
-- are about the same subject, which is exactly what similarity measures.
-- Jurisdiction, tier scope and effective dates are filters, not fuzzy matches,
-- so they belong in columns and in the WHERE clause.

create extension if not exists vector;

create schema if not exists knowledge;


create table knowledge.documents (
    document_id     bigint generated always as identity primary key,
    slug            text        not null unique,
    title           text        not null,
    doc_type        text        not null check (doc_type in
                        ('policy','playbook','guideline','postmortem','memo')),

    -- Filters. 'GLOBAL' rather than null so a query never has to decide what a
    -- missing jurisdiction means.
    jurisdiction    text        not null default 'GLOBAL',
    tier_scope      text[]      not null default '{}',   -- empty = all tiers
    channel_scope   text[]      not null default '{}',   -- empty = all channels

    -- A policy that has been replaced must not be quoted as current. Retrieval
    -- filters on these, which is the single most common way a knowledge base
    -- gives a confidently outdated answer.
    effective_from  date        not null,
    effective_to    date            null,
    supersedes      text            null references knowledge.documents(slug),

    body            text        not null,
    created_at      timestamptz not null default now()
);
create index documents_type_idx on knowledge.documents (doc_type, jurisdiction);
create index documents_effective_idx on knowledge.documents (effective_from, effective_to);
comment on table knowledge.documents is
    'Written L-Mart knowledge. effective_to is null while a document is current.';


-- Chunks, because a whole document is usually the wrong retrieval unit: a long
-- policy answers several different questions in different sections.
create table knowledge.chunks (
    chunk_id        bigint generated always as identity primary key,
    document_id     bigint      not null references knowledge.documents on delete cascade,
    ordinal         int         not null,
    heading         text            null,
    text            text        not null,

    -- 384 dimensions: BAAI/bge-small-en-v1.5. The model is recorded next to the
    -- vector because vectors from different models are not comparable, and
    -- mixing them in one index degrades every result silently.
    embedding       vector(384)     null,
    embedding_model text            null,

    -- Lexical half of hybrid search. Generated rather than maintained by
    -- trigger: it cannot drift from the text it indexes.
    tsv tsvector generated always as
        (to_tsvector('english', coalesce(heading,'') || ' ' || text)) stored,

    unique (document_id, ordinal)
);
create index chunks_tsv_idx on knowledge.chunks using gin (tsv);
-- IVFFlat needs training data to be worth building and this corpus is small
-- enough that an exact scan is faster than an approximate one. Revisit if the
-- corpus grows by an order of magnitude.
create index chunks_document_idx on knowledge.chunks (document_id);
comment on table knowledge.chunks is
    'Retrieval units. embedding_model records which model produced the vector; '
    'never query a chunk with a different model than indexed it.';
