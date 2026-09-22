"""Load the corpus into Postgres: parse, chunk, embed, store.

CHUNKING IS BY MARKDOWN SECTION, not by a fixed character count. These documents
are written with headings that mark genuine topic boundaries -- a policy's
"Discount ceilings" section answers a different question from its "Consent"
section. Splitting on a character count would cut through those boundaries at
arbitrary points and put half an answer in each of two chunks.

Run:  python -m knowledge.ingest
      python -m knowledge.ingest --reset
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

from core import db

from . import embeddings

CORPUS = pathlib.Path(__file__).parent / "corpus"
FRONT_MATTER = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.S)
# A heading starts a new chunk; the text before the first heading is the lead.
SECTION = re.compile(r"^## +(.+)$", re.M)


def parse(path: pathlib.Path) -> tuple[dict, str]:
    match = FRONT_MATTER.match(path.read_text())
    if not match:
        raise ValueError(f"{path.name} has no front matter")
    return json.loads(match.group(1)), match.group(2).strip()


def chunk(body: str) -> list[tuple[str | None, str]]:
    """Split into (heading, text). Sections shorter than a sentence are merged
    into the previous one -- a two-word chunk retrieves noisily and tells the
    reader nothing."""
    positions = [(m.start(), m.group(1)) for m in SECTION.finditer(body)]
    if not positions:
        return [(None, body)]

    chunks: list[tuple[str | None, str]] = []
    lead = body[: positions[0][0]].strip()
    if lead:
        chunks.append((None, lead))

    for index, (start, heading) in enumerate(positions):
        end = positions[index + 1][0] if index + 1 < len(positions) else len(body)
        text = body[start:end].split("\n", 1)[1].strip() if "\n" in body[start:end] else ""
        if not text:
            continue
        if len(text) < 80 and chunks:
            previous_heading, previous_text = chunks[-1]
            chunks[-1] = (previous_heading,
                          f"{previous_text}\n\n## {heading}\n{text}")
        else:
            chunks.append((heading, text))
    return chunks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true",
                        help="delete existing documents first")
    args = parser.parse_args()

    files = sorted(CORPUS.glob("*.md"))
    if not files:
        print("No corpus files. Run: python -m knowledge.build_corpus",
              file=sys.stderr)
        return 1

    embedder = embeddings.create()
    print(f"embedding with {embedder.model} ({embedder.dimension}-d)")

    with db.direct_connection() as conn:
        if args.reset:
            conn.execute("truncate knowledge.documents cascade")
            print("  cleared existing documents")

        total_chunks = 0
        for path in files:
            meta, body = parse(path)
            # supersedes references another slug, so insert without it first and
            # wire it up afterwards -- otherwise ingestion order would matter.
            supersedes = meta.pop("supersedes", None)
            document_id = conn.execute("""
                insert into knowledge.documents
                    (slug, title, doc_type, jurisdiction, tier_scope,
                     channel_scope, effective_from, effective_to, body)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                on conflict (slug) do update set
                    title = excluded.title, body = excluded.body,
                    effective_to = excluded.effective_to
                returning document_id
            """, (meta["slug"], meta["title"], meta["doc_type"],
                  meta.get("jurisdiction", "GLOBAL"), meta.get("tier_scope", []),
                  meta.get("channel_scope", []), meta["effective_from"],
                  meta.get("effective_to"), body)).fetchone()[0]

            conn.execute("delete from knowledge.chunks where document_id = %s",
                         (document_id,))
            sections = chunk(body)
            # The title is prepended to every chunk's embedded text: a section
            # headed "Rationale" is meaningless without knowing which document
            # it rationalises, and the query will mention the subject, not the
            # heading.
            vectors = embedder.embed_documents(
                [f"{meta['title']}. {heading or ''}. {text}"
                 for heading, text in sections])
            for ordinal, ((heading, text), vector) in enumerate(
                    zip(sections, vectors)):
                conn.execute("""
                    insert into knowledge.chunks
                        (document_id, ordinal, heading, text, embedding,
                         embedding_model)
                    values (%s,%s,%s,%s,%s,%s)
                """, (document_id, ordinal, heading, text, str(vector),
                      embedder.model))
            total_chunks += len(sections)
            print(f"  {len(sections):>2} chunks  {meta['slug']}")

        for path in files:
            meta, _ = parse(path)
            if meta.get("supersedes"):
                conn.execute(
                    "update knowledge.documents set supersedes=%s where slug=%s",
                    (meta["supersedes"], meta["slug"]))
        conn.commit()

    print(f"\n{len(files)} documents, {total_chunks} chunks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
