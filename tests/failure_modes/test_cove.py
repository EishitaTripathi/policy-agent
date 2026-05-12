"""
TESTS-FOR: policy_agent.cove (D5 Stage 3 — Chain-of-Verification)
PURPOSE: Validate CoVe's scope decisions and verdict shape using a
  monkey-patched judge (no real LLM calls in unit tests).

We test:
- Scope: should_run_cove decisions for various (tier, decision) tuples
  under different COVE_TIER_SCOPE values.
- Skip-when-disabled: COVE_ENABLED=false → invoked=False.
- Skip-when-no-citations: response with empty citations → invoked=False.
- Aligned-when-judge-supports: synthetic judge returns supports=True for
  all questions → CoVeVerdict.aligned == True.
- Drift-when-judge-rejects: synthetic judge returns supports=False →
  CoVeVerdict.aligned == False with divergences.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from policy_agent.cove import CoVeVerdict, should_run_cove, verify
from policy_agent.retrieval import RetrievedChunk
from policy_agent.schema import AgentResponse, Citation


def _response_with_citations(decision: str = "deny") -> AgentResponse:
    return AgentResponse(
        decision=decision,  # type: ignore[arg-type]
        action="I cannot share Sarah Chen's salary per §4.2. Please contact HR.",
        tool_calls=[],
        citations=[Citation(section_id="4.2", quote="must not access ... compensation")],
        reasoning="Salary is barred by §4.2.",
        escalation=None,
        cost_assessment=None,
    )


def _chunk(section_id: str, body: str) -> RetrievedChunk:
    return RetrievedChunk(
        section_id=section_id,
        parent_section=section_id.split(".")[0],
        section_title="Test Section",
        body=body,
        action_verb="must-not",
    )


# -------- Scope tests (no LLM) --------


def test_scope_disabled_by_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("COVE_ENABLED", raising=False)
    run, reason = should_run_cove("Grey", "deny")
    assert not run
    assert "false" in reason.lower()


def test_scope_grey_runs_when_enabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COVE_ENABLED", "true")
    monkeypatch.setenv("COVE_TIER_SCOPE", "Grey,Blue:deny,Blue:escalate")
    run, _ = should_run_cove("Grey", "allow")
    assert run


def test_scope_blue_allow_skipped(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COVE_ENABLED", "true")
    monkeypatch.setenv("COVE_TIER_SCOPE", "Grey,Blue:deny,Blue:escalate")
    run, reason = should_run_cove("Blue", "allow")
    assert not run
    assert "scope" in reason.lower()


def test_scope_blue_deny_compound_match(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COVE_ENABLED", "true")
    monkeypatch.setenv("COVE_TIER_SCOPE", "Grey,Blue:deny,Blue:escalate")
    run, _ = should_run_cove("Blue", "deny")
    assert run


# -------- Skip cases (still no LLM) --------


def test_verify_skipped_when_disabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COVE_ENABLED", "false")
    v = verify(_response_with_citations(), [_chunk("4.2", "must not...")], tier="Grey")
    assert not v.invoked
    assert "false" in v.skipped_reason.lower()


def test_verify_skipped_when_no_citations(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COVE_ENABLED", "true")
    monkeypatch.setenv("COVE_TIER_SCOPE", "Grey")
    r = AgentResponse(
        decision="allow",
        action="OK",
        tool_calls=[],
        citations=[],
        reasoning="No policy involved.",
        escalation=None,
        cost_assessment=None,
    )
    v = verify(r, [], tier="Grey")
    assert not v.invoked
    assert "citations" in v.skipped_reason.lower()


# -------- Aligned + drift cases (monkey-patched judge) --------


@dataclass
class _FakeChatResult:
    content: str
    raw: dict[str, Any]
    model: str = "test-judge"

    def parse_json(self) -> Any:
        import json
        return json.loads(self.content)


def _make_fake_judge(question_response: str, verify_supports: bool):
    """Returns a function that mimics judge_chat. Returns a question-gen
    payload on the first call and per-question verify payloads after."""
    calls = {"n": 0}

    def fake_judge_chat(messages: list, **kwargs: Any) -> _FakeChatResult:
        calls["n"] += 1
        if calls["n"] == 1:
            # question generation
            return _FakeChatResult(content=question_response, raw={})
        # subsequent calls: verification
        import json
        return _FakeChatResult(
            content=json.dumps({"supports": verify_supports, "reason": "test"}),
            raw={},
        )

    return fake_judge_chat


def test_verify_aligned_when_judge_supports(monkeypatch: pytest.MonkeyPatch):
    """All claim-verifications return supports=True → aligned=True."""
    monkeypatch.setenv("COVE_ENABLED", "true")
    monkeypatch.setenv("COVE_TIER_SCOPE", "Grey")
    fake = _make_fake_judge(
        question_response='{"questions": ["Q1: Does §4.2 forbid disclosure?", "Q2: Is alt path valid?"]}',
        verify_supports=True,
    )
    monkeypatch.setattr("policy_agent.llm.judge_chat", fake)
    v = verify(
        _response_with_citations(),
        [_chunk("4.2", "must not disclose compensation")],
        tier="Grey",
        num_questions=2,
    )
    assert v.invoked
    assert v.aligned
    assert len(v.questions) == 2
    assert not v.divergences


def test_verify_drift_when_judge_rejects(monkeypatch: pytest.MonkeyPatch):
    """Verifier rejects → aligned=False + divergences populated."""
    monkeypatch.setenv("COVE_ENABLED", "true")
    monkeypatch.setenv("COVE_TIER_SCOPE", "Grey")
    fake = _make_fake_judge(
        question_response='{"questions": ["Q1: Does the cited text support the decision?"]}',
        verify_supports=False,
    )
    monkeypatch.setattr("policy_agent.llm.judge_chat", fake)
    v = verify(
        _response_with_citations(),
        [_chunk("4.2", "different text that does NOT support")],
        tier="Grey",
        num_questions=1,
    )
    assert v.invoked
    assert not v.aligned
    assert len(v.divergences) >= 1
