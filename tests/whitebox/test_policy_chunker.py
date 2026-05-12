"""
TESTS-FOR: policy_agent.policy_chunker
PURPOSE: The chunker tags §6 ("General Conduct") clauses as
  ``is_general_guideline=True`` so the retrieval layer can always-on
  them regardless of vector similarity. Everything else must remain
  ``False`` — otherwise topic-specific clauses would leak into the
  always-on set and dilute every prompt.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from policy_agent.policy_chunker import chunk_policy_file


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_POLICY_PATHS = [
    _REPO_ROOT / "policies" / "seed_policy.md",
    _REPO_ROOT / "policies" / "gaggia-it-policy.md",
]


@pytest.mark.parametrize("policy_path", _POLICY_PATHS)
def test_section_6_clauses_tagged_as_general_guideline(policy_path: Path):
    if not policy_path.exists():
        pytest.skip(f"{policy_path} not present")
    clauses = chunk_policy_file(policy_path)
    sec6 = [c for c in clauses if c.parent_section == "6"]
    assert sec6, "expected at least one §6 clause"
    for c in sec6:
        assert c.is_general_guideline is True, (
            f"§{c.section_id} should be tagged general_guideline; got False"
        )


@pytest.mark.parametrize("policy_path", _POLICY_PATHS)
def test_non_section_6_clauses_not_tagged(policy_path: Path):
    if not policy_path.exists():
        pytest.skip(f"{policy_path} not present")
    clauses = chunk_policy_file(policy_path)
    non6 = [c for c in clauses if c.parent_section != "6"]
    assert non6, "expected clauses outside §6"
    for c in non6:
        assert c.is_general_guideline is False, (
            f"§{c.section_id} (parent §{c.parent_section}) should NOT be "
            "tagged general_guideline"
        )


def test_general_guideline_round_trips_through_chroma_metadata():
    """The flag must survive Chroma's primitive-only metadata constraint."""
    clauses = chunk_policy_file(_POLICY_PATHS[0])
    sec6 = next(c for c in clauses if c.parent_section == "6")
    meta = sec6.to_chroma_metadata()
    assert meta["is_general_guideline"] is True
