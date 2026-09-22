"""Turning text into vectors, behind a provider boundary.

DEFAULT MODEL: BAAI/bge-small-en-v1.5, 384 dimensions, over the Hugging Face
inference API. Chosen because it is already in use and measured in the sibling
RAG project (recall@3 = 0.826 on a 28-question gold set), which gives a
reference point: if L-Mart's retrieval lands far below that, the problem is the
chunking or the gold set, not the model.

Several details below are carried over from that project rather than
rediscovered, because each was learned the hard way:

  * HTTP 503 means the model is loading, not that the request failed. It is
    worth one patient retry.
  * Rows are scaled to unit length, so a dot product IS the cosine similarity
    and Postgres can use the cheaper operator.
  * The query prefix is EMPTY. BGE documents an instruction to prepend to
    queries; measured on the gold set it gave identical recall and a slightly
    narrower gap between answerable and unanswerable questions, so it is
    deliberately omitted rather than forgotten.

DIMENSION IS DERIVED FROM THE MODEL, never configured separately. Two settings
that must agree are two settings that will eventually disagree, and the symptom
of a mismatch is a silent quality collapse rather than an error.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from core import config
from core.errors import ActionableError

HF_URL = ("https://router.huggingface.co/hf-inference/models/{model}"
          "/pipeline/feature-extraction")

# model -> dimension. A model not listed here is refused rather than guessed:
# the column is vector(384) and a wrong-width vector fails at insert time with
# an error that says nothing about the cause.
KNOWN_MODELS = {
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
}
DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"

BATCH_SIZE = 32          # measured: 32 texts return in about half a second
TIMEOUT_SECONDS = 60
QUERY_PREFIX = ""        # see module docstring


class EmbeddingError(ActionableError):
    """Embeddings could not be produced."""


@dataclass(frozen=True)
class Embedder:
    model: str
    dimension: int
    token: str

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._embed([QUERY_PREFIX + text])[0]

    def _embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), BATCH_SIZE):
            vectors.extend(self._post(texts[start:start + BATCH_SIZE]))
        return vectors

    def _post(self, texts: list[str]) -> list[list[float]]:
        request = urllib.request.Request(
            HF_URL.format(model=self.model),
            data=json.dumps({"inputs": texts}).encode(),
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"},
            method="POST")

        for attempt in range(3):
            try:
                with urllib.request.urlopen(request,
                                            timeout=TIMEOUT_SECONDS) as response:
                    raw = json.loads(response.read())
                break
            except urllib.error.HTTPError as exc:
                body = exc.read()[:200].decode(errors="replace")
                # 503 is a cold start, not a failure.
                if exc.code == 503 and attempt < 2:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise EmbeddingError(
                    f"Hugging Face returned {exc.code}: {body}") from exc
            except Exception as exc:                        # noqa: BLE001
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise EmbeddingError(f"Embedding request failed: {exc}") from exc
        else:                                               # pragma: no cover
            raise EmbeddingError("Embedding request failed after retries")

        if len(raw) != len(texts) or len(raw[0]) != self.dimension:
            raise EmbeddingError(
                f"{self.model} returned {len(raw)}x{len(raw[0]) if raw else 0}; "
                f"expected {len(texts)}x{self.dimension}")
        return [_unit(vector) for vector in raw]


def _unit(vector: list[float]) -> list[float]:
    """Scale to unit length so a dot product is the cosine similarity."""
    magnitude = sum(value * value for value in vector) ** 0.5
    if magnitude == 0:
        return vector
    return [value / magnitude for value in vector]


def create(model: str | None = None) -> Embedder:
    chosen = model or config.get("EMBEDDING_MODEL", DEFAULT_MODEL)
    if chosen not in KNOWN_MODELS:
        raise EmbeddingError(
            f"Unknown embedding model {chosen!r}. Known: "
            f"{', '.join(KNOWN_MODELS)}. Add it to KNOWN_MODELS with its "
            f"dimension -- the dimension must never be configured separately.")
    return Embedder(model=chosen, dimension=KNOWN_MODELS[chosen],
                    token=config.require("HF_TOKEN"))
