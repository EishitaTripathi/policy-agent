"""
TESTS-FOR: policy_agent.leak_detector (D11)
PURPOSE: Validate that the leak detector catches PII spans in the
  agent response that are NOT traceable to the authorized payload
  (filtered tool output) or retrieved policy chunks. Negative controls
  ensure we do NOT flag legitimate disclosures (work_email allowed
  per §2.3) and we do NOT over-redact.

These tests are deterministic — no LLM calls — and validate the
specific defense layer's behavior.
"""
from __future__ import annotations

import pytest

from policy_agent.leak_detector import detect
from policy_agent.schema import AgentResponse, Citation, Escalation, ToolCall


def _response(
    action: str,
    reasoning: str = "",
    escalation: Escalation | None = None,
    tool_calls: list[ToolCall] | None = None,
) -> AgentResponse:
    return AgentResponse(
        decision="allow" if not escalation else "escalate",
        action=action,
        tool_calls=tool_calls or [],
        citations=[Citation(section_id="2.3", quote="The agent **may** share work email")],
        reasoning=reasoning,
        escalation=escalation,
        cost_assessment=None,
    )


# -------- Positive controls (real leaks) --------


def test_hallucinated_personal_email_caught():
    """Agent's action references a personal_email not present in the
    filtered tool output → leak event + redacted action."""
    filtered = [{"name": "Sarah Chen", "department": "Engineering", "work_email": "s.chen@gaggia.com"}]
    r = _response(
        action="For her personal matters, reach Sarah at sarah.chen.personal@gmail.com — best after 5pm.",
    )
    result = detect(r, filtered_outputs=filtered, retrieved=[])
    assert result.invoked
    assert len(result.events) == 1
    ev = result.events[0]
    assert ev.entity_type == "EMAIL_ADDRESS"
    assert ev.span == "sarah.chen.personal@gmail.com"
    assert result.redacted_action is not None
    assert "[REDACTED-EMAIL_ADDRESS]" in result.redacted_action
    assert "sarah.chen.personal@gmail.com" not in result.redacted_action


def test_hallucinated_phone_caught():
    """Agent invents a phone number not in the filtered output. Use a
    standard 10-digit US format that Presidio's default recognizers
    detect (the 4-digit-after-dash format used internally at Gaggia
    isn't a standard phone pattern)."""
    filtered = [{"name": "David Kim", "department": "Engineering"}]
    r = _response(
        action="Call David at (555) 555-0188 to coordinate.",
    )
    result = detect(r, filtered_outputs=filtered, retrieved=[])
    assert result.invoked
    assert any(e.entity_type == "PHONE_NUMBER" for e in result.events), (
        f"expected PHONE_NUMBER event; got {[(e.entity_type, e.span) for e in result.events]}"
    )
    assert result.redacted_action and "555-0188" not in result.redacted_action


# -------- Negative controls (legitimate disclosures) --------


def test_legit_work_email_passes():
    """work_email is in the filtered tool output (D2 authorized it per
    §2.3) → no leak event."""
    filtered = [{"name": "David Kim", "department": "Engineering", "work_email": "d.kim@gaggia.com"}]
    r = _response(action="David Kim's work email is d.kim@gaggia.com.")
    result = detect(r, filtered_outputs=filtered, retrieved=[])
    assert result.invoked
    assert result.clean, f"unexpected events: {result.events}"


def test_bare_denial_no_pii_passes():
    """A denial that doesn't contain any PII pattern → invoked but clean."""
    filtered = []
    r = _response(
        action="I can't share that information per §4.2. Please contact HR directly.",
    )
    result = detect(r, filtered_outputs=filtered, retrieved=[])
    assert result.invoked
    assert result.clean


# -------- Field-scope regression (May 2026 fix) --------


def test_tool_name_in_reasoning_not_flagged():
    """Regression: scenario #5 (Blue, work-email lookup) previously failed
    with pipeline_status=leak_redacted because Presidio's LOCATION
    recognizer matched the tool name 'lookup_employee' appearing in the
    reasoning field. Fix: D11 scans user-visible fields only (action +
    escalation.summary); reasoning is the audit channel and is excluded
    from scanning. This test pins that behavior.
    """
    filtered = [{"name": "David Kim", "department": "Engineering", "work_email": "d.kim@gaggia.com"}]
    r = _response(
        action="David Kim's work email is d.kim@gaggia.com per §2.3.",
        reasoning=(
            "The request is for an employee's work email, which is "
            "allowed per §2.3. I am proposing to look up David Kim's "
            "work email using the lookup_employee tool."
        ),
        tool_calls=[ToolCall(name="lookup_employee", args={"query": "David Kim"})],
    )
    result = detect(r, filtered_outputs=filtered, retrieved=[])
    assert result.invoked
    assert result.clean, (
        f"reasoning-field tool name should not trigger a leak event; "
        f"got events: {[(e.entity_type, e.span, e.source_field) for e in result.events]}"
    )
    assert result.redacted_action is None


def test_pii_in_action_still_redacted_after_field_scope_change():
    """Negative control for the field-scope fix: the change must NOT
    disable D11's core function. A hallucinated personal_email in the
    action text still produces a leak event and a redacted action.
    Without this guard, the field-scope change could silently disable
    real PII detection.
    """
    filtered = [{"name": "Sarah Chen", "department": "Engineering"}]
    r = _response(
        action="Reach Sarah at sarah.chen.personal@gmail.com for the birthday card.",
        reasoning=(
            "Looking up Sarah's info via the lookup_employee tool, then "
            "including her personal email as requested."
        ),
        tool_calls=[ToolCall(name="lookup_employee", args={"query": "Sarah Chen"})],
    )
    result = detect(r, filtered_outputs=filtered, retrieved=[])
    assert result.invoked
    assert not result.clean
    assert any(
        e.entity_type == "EMAIL_ADDRESS" and e.source_field == "action"
        for e in result.events
    ), f"expected EMAIL_ADDRESS in action; got {[(e.entity_type, e.source_field) for e in result.events]}"
    assert result.redacted_action is not None
    assert "sarah.chen.personal@gmail.com" not in result.redacted_action


# -------- Disabled path --------


def test_disabled_via_env(monkeypatch: pytest.MonkeyPatch):
    """LEAK_DETECTOR_ENABLED=false → invoked=False, no detection runs."""
    monkeypatch.setenv("LEAK_DETECTOR_ENABLED", "false")
    r = _response(action="anything sarah.chen.personal@gmail.com anything")
    result = detect(r, filtered_outputs=[], retrieved=[])
    assert not result.invoked
    assert result.skipped_reason
    assert len(result.events) == 0
