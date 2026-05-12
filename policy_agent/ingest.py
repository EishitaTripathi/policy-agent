"""
COMPONENT: ingest
DESIGN-REF: D4 (Policy retrieval — vector store population)
PURPOSE: Populate the ChromaDB collection from the parsed policy chunks.
  One Chroma document per leaf clause; metadata carries section_id,
  parent_section, section_title, action_verb, cross_refs (JSON), is_seed.
  Embeddings come from sentence-transformers/all-MiniLM-L6-v2 (local,
  no API key, ~90MB).
PROBLEM-STATEMENT REQ (verbatim): >
  "Policy & Rule Representation — With a 15-30 page policy document, how
  you store, index, and retrieve rules at runtime matters. ... How you
  structure the policy for retrieval — vector store, graph, hybrid, rule
  engine, or something else — is one of the most consequential design
  decisions in this project."
EXPECTED INPUT: policies/gaggia-it-policy.md (path resolved from CHROMA_PATH env)
EXPECTED OUTPUT: persistent Chroma collection at CHROMA_PATH
UPSTREAM: invoked manually via `python -m policy_agent.ingest`
DOWNSTREAM: chromadb, sentence-transformers, policy_chunker
COMPONENT TESTS: tests/whitebox/test_ingest.py (chunk count, metadata shape)
SCENARIO COVERAGE: foundation for all 21 (every scenario retrieves chunks)
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from policy_agent.policy_chunker import Clause, chunk_policy_file

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_POLICY = REPO_ROOT / "policies" / "gaggia-it-policy.md"
DEFAULT_CHROMA_PATH = REPO_ROOT / ".chroma"


def _ensure_env() -> None:
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    else:
        load_dotenv(REPO_ROOT / ".env.example")


def _embedder():
    """Lazy-load the local sentence-transformer model. ~90MB on first use."""
    from sentence_transformers import SentenceTransformer

    name = os.environ.get("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    return SentenceTransformer(name)


def _client(persist_path: Path):
    import chromadb

    persist_path.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(persist_path))


def _embed_text(clause: Clause) -> str:
    """The text we embed: section title + clause body. Title gives the
    embedder topical context; body carries the actual rule. The clause
    ID itself is intentionally not in the embedding (it's noise for
    semantic search) but is in metadata for citation."""
    return f"{clause.section_title}\n{clause.body}"


def main(argv: list[str] | None = None) -> int:
    _ensure_env()
    policy_path = DEFAULT_POLICY
    persist_path = Path(os.environ.get("CHROMA_PATH", DEFAULT_CHROMA_PATH))
    collection_name = os.environ.get("CHROMA_COLLECTION", "gaggia_policy")

    print(f"[1/4] Parsing {policy_path.relative_to(REPO_ROOT)}...")
    clauses = chunk_policy_file(policy_path)
    print(f"  - {len(clauses)} clauses across {len({c.parent_section for c in clauses})} sections")

    print(f"[2/4] Loading embedder ({os.environ.get('EMBEDDING_MODEL', 'all-MiniLM-L6-v2')})...")
    t0 = time.time()
    embedder = _embedder()
    texts = [_embed_text(c) for c in clauses]
    embeddings = embedder.encode(texts, show_progress_bar=False, normalize_embeddings=True).tolist()
    print(f"  - embedded {len(embeddings)} chunks in {time.time() - t0:.1f}s")

    print(f"[3/4] Connecting to Chroma at {persist_path}...")
    client = _client(persist_path)
    # Recreate the collection so re-ingest is idempotent.
    try:
        client.delete_collection(collection_name)
    except Exception:
        pass
    collection = client.create_collection(
        name=collection_name,
        metadata={"hnsw:space": "cosine"},
    )

    print("[4/4] Adding chunks to collection...")
    ids = [f"clause-{c.section_id}" for c in clauses]
    documents = [c.body for c in clauses]
    metadatas = [c.to_chroma_metadata() for c in clauses]
    collection.add(
        ids=ids,
        documents=documents,
        embeddings=embeddings,
        metadatas=metadatas,
    )
    print(f"  - inserted {collection.count()} chunks into '{collection_name}'.")

    # Sanity query: a known-distinctive seed clause.
    print("\n[sanity] retrieving on a known query 'reset password for executive account':")
    sample = collection.query(
        query_embeddings=embedder.encode(
            ["reset password for executive account"],
            normalize_embeddings=True,
        ).tolist(),
        n_results=3,
    )
    for sec_id, doc in zip(sample["ids"][0], sample["documents"][0]):
        snippet = doc[:120].replace("\n", " ")
        print(f"  - {sec_id}: {snippet}...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
