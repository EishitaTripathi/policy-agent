"""
TESTS-FOR: policy_agent.retrieval — general-guidelines pre-retrieval

PURPOSE: The agent prompt no longer paraphrases §6 ("General Conduct").
  Instead, retrieval prepends every §6 clause to the chunk set so the
  agent reads them through the same channel as topic-specific clauses.
  These tests pin the contract:
    - ``fetch_general_guidelines()`` returns exactly the §6 clauses.
    - ``retrieve_with_general_guidelines()`` prepends them and dedupes
      against vector hits so a single section never appears twice.

Skipped if the Chroma index isn't ingested.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_CHROMA_PATH = _REPO_ROOT / ".chroma"

pytestmark = pytest.mark.skipif(
    not _CHROMA_PATH.exists(),
    reason="Chroma index missing — run `python -m policy_agent.ingest` first.",
)


def test_fetch_general_guidelines_returns_section_6_only():
    from policy_agent.retrieval import fetch_general_guidelines

    # Cached at module scope — clear so we re-read after fresh ingestion.
    fetch_general_guidelines.cache_clear()
    chunks = fetch_general_guidelines()

    assert chunks, "expected at least one general-guideline chunk in the index"
    for c in chunks:
        assert c.is_general_guideline is True
        assert c.parent_section == "6", (
            f"general-guideline chunk has parent §{c.parent_section}, expected §6"
        )

    section_ids = {c.section_id for c in chunks}
    # The seed defines §6.1, §6.2, §6.3 — all three must be present.
    assert {"6.1", "6.2", "6.3"}.issubset(section_ids), (
        f"missing core §6 clauses; got {sorted(section_ids)}"
    )


def test_retrieve_with_general_guidelines_prepends_and_dedupes():
    from policy_agent.retrieval import (
        fetch_general_guidelines,
        retrieve_with_general_guidelines,
    )

    fetch_general_guidelines.cache_clear()
    general_ids = {c.section_id for c in fetch_general_guidelines()}

    # A query topically unrelated to §6 — vector retrieval will surface
    # §1.x / §2.x clauses, and the wrapper must prepend §6 anyway.
    merged = retrieve_with_general_guidelines(
        "How do I reset my password?", rerank_top_n=3
    )
    merged_ids = [c.section_id for c in merged]

    # No section appears twice.
    assert len(merged_ids) == len(set(merged_ids)), (
        f"duplicate section_ids in merged result: {merged_ids}"
    )
    # All general-guideline clauses are present.
    assert general_ids.issubset(set(merged_ids)), (
        f"missing general guidelines in merged result; "
        f"got {merged_ids}, expected {sorted(general_ids)} ⊆ result"
    )
    # General-guideline chunks come first (deterministic position).
    head = merged_ids[: len(general_ids)]
    assert set(head) == general_ids, (
        f"general guidelines should occupy the head; got head={head}"
    )
