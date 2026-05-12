"""
COMPONENT: retrieval
DESIGN-REF: D4 (Policy retrieval — vector search + rerank)
PURPOSE: Query the ChromaDB policy index and return the top-N relevant
  clauses for the agent. Two-stage: vector search (top-k via cosine) then
  cross-encoder rerank (BAAI/bge-reranker-base) for precision before
  passing chunks to the agent. Reranker is lazy-loaded; falls back to
  vector-only if disabled.
PROBLEM-STATEMENT REQ (verbatim): >
  "Policy & Rule Representation — ... How you structure the policy for
  retrieval ... is one of the most consequential design decisions in
  this project. What matters is that you can explain why your choice
  works (and where it breaks)."
EXPECTED INPUT: query string + optional metadata filters
EXPECTED OUTPUT: list[RetrievedChunk] sorted by relevance
UPSTREAM: policy_agent.agent (D1), policy_agent.red_path (D1), tests
DOWNSTREAM: chromadb, sentence-transformers (encoder + cross-encoder)
COMPONENT TESTS: tests/whitebox/test_retrieval.py
SCENARIO COVERAGE: foundation for all 21 scenarios.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CHROMA_PATH = REPO_ROOT / ".chroma"


def _ensure_env() -> None:
    env = REPO_ROOT / ".env"
    if env.exists():
        load_dotenv(env)
    else:
        load_dotenv(REPO_ROOT / ".env.example")


@dataclass
class RetrievedChunk:
    section_id: str
    parent_section: str
    section_title: str
    body: str
    action_verb: str | None
    cross_refs: list[str] = field(default_factory=list)
    is_seed: bool = False
    score: float = 0.0  # higher = more relevant (post-rerank if reranked)
    distance: float | None = None  # cosine distance from vector search

    def citation_label(self) -> str:
        """Human-readable label for display in the agent's response."""
        return f"§{self.section_id} ({self.section_title})"


@lru_cache(maxsize=1)
def _embedder():
    from sentence_transformers import SentenceTransformer

    name = os.environ.get("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    return SentenceTransformer(name)


@lru_cache(maxsize=1)
def _reranker():
    from sentence_transformers import CrossEncoder

    name = os.environ.get("RERANKER_MODEL", "BAAI/bge-reranker-base")
    return CrossEncoder(name)


@lru_cache(maxsize=1)
def _collection():
    import chromadb

    _ensure_env()
    persist_path = Path(os.environ.get("CHROMA_PATH", DEFAULT_CHROMA_PATH))
    collection_name = os.environ.get("CHROMA_COLLECTION", "gaggia_policy")
    if not persist_path.exists():
        raise RuntimeError(
            f"Chroma path {persist_path} does not exist. "
            "Run `python -m policy_agent.ingest` first."
        )
    client = chromadb.PersistentClient(path=str(persist_path))
    try:
        return client.get_collection(collection_name)
    except Exception as exc:
        raise RuntimeError(
            f"Collection '{collection_name}' not found. "
            "Run `python -m policy_agent.ingest` to create it."
        ) from exc


def _to_chunk(meta: dict[str, Any], document: str, distance: float | None) -> RetrievedChunk:
    return RetrievedChunk(
        section_id=meta.get("section_id", ""),
        parent_section=meta.get("parent_section", ""),
        section_title=meta.get("section_title", ""),
        body=document,
        action_verb=meta.get("action_verb") or None,
        cross_refs=json.loads(meta.get("cross_refs_json", "[]")),
        is_seed=bool(meta.get("is_seed", False)),
        distance=distance,
    )


def retrieve(
    query: str,
    *,
    top_k: int | None = None,
    rerank_top_n: int | None = None,
    filters: dict | None = None,
    use_reranker: bool = True,
) -> list[RetrievedChunk]:
    """Retrieve the top-N policy clauses for `query`.

    Two-stage:
      1. Cosine top-k from the vector store.
      2. If `use_reranker`, BAAI/bge-reranker-base scores those k and we
         keep the top-n (default RERANK_TOP_K from env or 5).

    `filters` is passed to Chroma as a `where` clause. Useful values:
      - {"is_seed": True}
      - {"parent_section": "4"}
      - {"action_verb": "must-not"}
    """
    _ensure_env()
    if top_k is None:
        top_k = int(os.environ.get("RETRIEVAL_TOP_K", "20"))
    if rerank_top_n is None:
        rerank_top_n = int(os.environ.get("RERANK_TOP_K", "5"))

    embedder = _embedder()
    qe = embedder.encode([query], normalize_embeddings=True).tolist()
    res = _collection().query(
        query_embeddings=qe,
        n_results=top_k,
        where=filters or None,
    )
    docs = res["documents"][0]
    metas = res["metadatas"][0]
    dists = res["distances"][0] if "distances" in res else [None] * len(docs)
    chunks = [_to_chunk(m, d, dist) for m, d, dist in zip(metas, docs, dists)]

    if use_reranker and len(chunks) > 1:
        reranker = _reranker()
        pairs = [(query, f"{c.section_title}. {c.body}") for c in chunks]
        scores = reranker.predict(pairs).tolist()
        for c, s in zip(chunks, scores):
            c.score = float(s)
        chunks.sort(key=lambda c: c.score, reverse=True)
        return chunks[:rerank_top_n]

    # No rerank: sort by ascending distance (closer = better) and take rerank_top_n.
    chunks.sort(key=lambda c: c.distance if c.distance is not None else 0.0)
    for c in chunks:
        c.score = -(c.distance or 0.0)
    return chunks[:rerank_top_n]


if __name__ == "__main__":
    import sys

    queries = [
        "Can I reset the password for the svc-deploy service account?",
        "What's Sarah Chen's salary?",
        "I need access to the legal-hold drive for an investigation",
        "How many PTO days do we get per year?",
        "I'm covering for a colleague on PTO and need access to the Design team's drive",
    ]
    for q in queries:
        print(f"\nQuery: {q}")
        for c in retrieve(q, rerank_top_n=3):
            print(
                f"  - §{c.section_id} [{c.action_verb}] (score={c.score:+.3f}): "
                f"{c.body[:90].replace(chr(10), ' ')}..."
            )
    sys.exit(0)
