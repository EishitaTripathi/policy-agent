"""
TESTS-FOR: policy_agent.dispatcher (D3 — Tier-tool allowlist)
PURPOSE: Verify the dispatcher enforces trust-tier rules deterministically:
  Red is blocked from every tool except escalate_to_human (with one
  argument-constrained exception for query_hr_database(query_type=policy));
  Blue/Grey get the full tool palette; the per-request audit log is
  populated.

This covers core requirement #2 (Trust Tier Enforcement). No LLM calls.
"""
from __future__ import annotations

import pytest

from policy_agent.dispatcher import CallerContext, Dispatcher


def _ctx(tier: str, employee_id: str | None = "EMP-1042", *, request_id: str = "req-1") -> CallerContext:
    return CallerContext(
        request_id=request_id,
        conversation_id=request_id,
        tier=tier,  # type: ignore[arg-type]
        employee_id=employee_id,
    )


# ---------- Red: blocked except escalate ----------


@pytest.mark.parametrize(
    "tool_name, args",
    [
        ("reset_password", {"employee_id": "EMP-2011"}),
        ("lookup_employee", {"query": "Sarah Chen"}),
        ("grant_file_access", {"employee_id": "EMP-1500", "drive_id": "DRV-design", "access_level": "read"}),
        ("query_hr_database", {"query_type": "individual", "employee_id": "EMP-1042"}),
    ],
)
def test_red_blocks_non_escalate_tools(tool_name: str, args: dict):
    d = Dispatcher()
    res = d.dispatch(_ctx("Red", request_id=f"red-{tool_name}"), tool_name, args)
    assert res.status == "rejected", (
        f"Red must not be authorized for {tool_name}; got {res.status} "
        f"(reason={res.rejection_reason!r})"
    )
    assert res.rejection_reason  # non-empty rationale


def test_red_allows_escalate_to_human():
    d = Dispatcher()
    res = d.dispatch(
        _ctx("Red", request_id="red-esc"),
        "escalate_to_human",
        {"reason": "Untrusted request", "conversation_summary": "summary"},
    )
    assert res.status == "authorized"
    assert res.output is not None
    assert "ticket_id" in res.output


def test_red_query_hr_policy_allowed_individual_rejected():
    """Argument-level constraint per policies/tier-tool-allowlist.yaml:
    Red may call query_hr_database only with query_type=policy."""
    d = Dispatcher()
    ok = d.dispatch(
        _ctx("Red", request_id="red-hr-policy"),
        "query_hr_database",
        {"query_type": "policy", "employee_id": "pto"},
    )
    assert ok.status == "authorized"

    bad = d.dispatch(
        _ctx("Red", request_id="red-hr-individual"),
        "query_hr_database",
        {"query_type": "individual", "employee_id": "EMP-1042"},
    )
    assert bad.status == "rejected"


# ---------- Blue / Grey: full palette ----------


@pytest.mark.parametrize("tier", ["Blue", "Grey"])
@pytest.mark.parametrize(
    "tool_name, args",
    [
        ("reset_password", {"employee_id": "EMP-2011"}),
        ("lookup_employee", {"query": "EMP-1042"}),
        (
            "grant_file_access",
            {"employee_id": "EMP-1500", "drive_id": "DRV-design", "access_level": "read"},
        ),
        ("query_hr_database", {"query_type": "policy", "employee_id": "pto"}),
        (
            "escalate_to_human",
            {"reason": "x", "conversation_summary": "y"},
        ),
    ],
)
def test_blue_and_grey_allowed_tools(tier: str, tool_name: str, args: dict):
    d = Dispatcher()
    res = d.dispatch(_ctx(tier, request_id=f"{tier}-{tool_name}"), tool_name, args)
    assert res.status == "authorized", (
        f"{tier} must be authorized for {tool_name}; got {res.status} "
        f"(reason={res.rejection_reason!r})"
    )


# ---------- Audit log surface (Req #5) ----------


def test_authorized_tools_log_records_per_request():
    """After dispatching, the per-request authorized list reflects exactly
    the authorized calls."""
    d = Dispatcher()
    ctx_a = _ctx("Blue", request_id="audit-a")
    ctx_b = _ctx("Red", request_id="audit-b")

    d.dispatch(ctx_a, "reset_password", {"employee_id": "EMP-2011"})
    d.dispatch(ctx_a, "lookup_employee", {"query": "EMP-1042"})
    d.dispatch(ctx_b, "reset_password", {"employee_id": "EMP-2011"})  # rejected

    assert sorted(d.authorized_tools("audit-a")) == ["lookup_employee", "reset_password"]
    assert d.authorized_tools("audit-b") == []
    assert len(d.rejected_attempts("audit-b")) == 1
    assert d.history("audit-a")[0].tool_name == "reset_password"


def test_unknown_tool_returns_tool_unknown():
    d = Dispatcher()
    res = d.dispatch(_ctx("Blue", request_id="unk"), "this_tool_does_not_exist", {})
    assert res.status == "tool_unknown"
    assert "unknown tool" in (res.rejection_reason or "").lower()
