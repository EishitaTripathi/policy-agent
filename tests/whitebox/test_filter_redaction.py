"""
TESTS-FOR: policy_agent.filter (D2 — tag-driven output filter)
PURPOSE: Verify that tool outputs are redacted per (tag, relationship)
  before they reach the user. This is core requirement #4 (Tool Output
  Filtering). Asserts the exact fields stripped for peer / self /
  manager_in_chain relationships against the lookup_employee and
  query_hr_database tools.

No LLM calls — exercises filter_output() and determine_relationship()
directly against the mock _ACCOUNTS fixture in policy_agent.tools.
"""
from __future__ import annotations

from policy_agent.filter import determine_relationship, filter_output
from policy_agent.tools import lookup_employee, query_hr_database


# ---------- determine_relationship() ----------


def test_relationship_self():
    assert determine_relationship("EMP-1042", "EMP-1042") == "self"


def test_relationship_manager_in_chain():
    # David Kim (EMP-1043) has Jordan Rivera (EMP-1100) as a direct report.
    assert determine_relationship("EMP-1043", "EMP-1100") == "manager_in_chain"


def test_relationship_peer():
    # Two engineers, neither is the other's manager.
    assert determine_relationship("EMP-3300", "EMP-1042") == "peer"


def test_relationship_unknown_requester_becomes_other():
    assert determine_relationship(None, "EMP-1042") == "other"
    assert determine_relationship("EMP-1042", None) == "other"


# ---------- lookup_employee filtering ----------


def test_lookup_employee_peer_redacts_personal_and_hr_fields():
    """Scenarios #6, #9, #10, #20: a peer must not see salary, performance,
    personal_email, personal_phone, home_address."""
    raw = lookup_employee("EMP-1042")  # Sarah Chen
    res = filter_output("lookup_employee", raw, relationship="peer")
    redacted = set(res.redacted_fields)
    must_redact = {
        "salary",
        "performance_rating",
        "personal_email",
        "personal_phone",
        "home_address",
    }
    assert must_redact.issubset(redacted), (
        f"missing redactions for peer: {must_redact - redacted}"
    )
    # Field-level: ensure they're absent from filtered_output.
    for field in must_redact:
        assert field not in res.filtered_output


def test_lookup_employee_peer_keeps_directory_fields():
    """§2.1 / §2.3 — name, department, title, manager, work_email, work_phone
    are directory info; a peer may see them."""
    raw = lookup_employee("EMP-1042")
    res = filter_output("lookup_employee", raw, relationship="peer")
    for field in ("name", "department", "title", "manager", "work_email", "work_phone"):
        assert field in res.filtered_output, (
            f"peer must see {field}; got filtered_output={sorted(res.filtered_output)}"
        )


def test_lookup_employee_self_keeps_personal_fields():
    """§15.3 (self-service carve-out): employee looking up their own record
    sees personal contact + compensation + performance."""
    raw = lookup_employee("EMP-1042")
    res = filter_output("lookup_employee", raw, relationship="self")
    for field in (
        "personal_email",
        "personal_phone",
        "home_address",
        "salary",
        "performance_rating",
    ):
        assert field in res.filtered_output, (
            f"self must see {field}; got filtered_output={sorted(res.filtered_output)}"
        )


def test_lookup_employee_manager_in_chain_sees_employment_status_not_compensation():
    """§4.4 grants a verified manager visibility into employment_status for
    a direct report, but §4.2 still bars compensation/performance."""
    raw = lookup_employee("EMP-1100")  # Jordan Rivera
    res = filter_output("lookup_employee", raw, relationship="manager_in_chain")
    if "employment_status" in raw:
        assert "employment_status" in res.filtered_output
    assert "salary" not in res.filtered_output
    assert "performance_rating" not in res.filtered_output


# ---------- query_hr_database filtering ----------


def test_query_hr_individual_peer_redacts_compensation_and_performance():
    """Scenario #9: a peer asks for someone else's individual HR record;
    compensation + performance + disciplinary must be stripped."""
    raw = query_hr_database("individual", "EMP-1042")
    res = filter_output("query_hr_database", raw, relationship="peer")
    for field in ("salary", "bonus_target", "performance_rating", "last_review", "disciplinary_actions"):
        if field in raw:
            assert field not in res.filtered_output, (
                f"peer must not see {field}; got filtered_output={sorted(res.filtered_output)}"
            )


def test_query_hr_policy_allowed_for_everyone():
    """§4.1: general HR policy answers (free text) are broadly disclosable."""
    raw = query_hr_database("policy", "pto")
    for rel in ("self", "manager_in_chain", "peer", "other"):
        res = filter_output("query_hr_database", raw, relationship=rel)  # type: ignore[arg-type]
        assert "result" in res.filtered_output, (
            f"{rel} should see hr_policy_text; got {sorted(res.filtered_output)}"
        )
