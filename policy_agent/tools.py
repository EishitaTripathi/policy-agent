"""
COMPONENT: tools
DESIGN-REF: D2 (field-tag annotations on tool schemas) + D3 (tool registry)
PURPOSE: The five mock tools the agent can call, plus a registry that
  declares each tool's response schema annotated with field-level tags
  (e.g., personal_email -> personal_contact, salary -> compensation).
  Tags are tool properties, not policy properties — so the filter ruleset
  (D2) can map (tag, relationship) -> allowed/denied without hardcoding
  policy section IDs in code.
PROBLEM-STATEMENT REQ (verbatim): >
  "1. reset_password — Reset an employee's account password
   2. lookup_employee — Look up an employee's profile by name or ID
   3. grant_file_access — Grant access to a shared drive
   4. query_hr_database — Query HR policies or individual employee records
   5. escalate_to_human — Escalate the conversation to a human IT operator"
EXPECTED INPUT: depends on tool — see each function's signature
EXPECTED OUTPUT: dict with realistic mock data matching the spec
UPSTREAM: policy_agent.dispatcher (only entry point for tool calls)
DOWNSTREAM: stdlib only (random, datetime, secrets) for mock generation
COMPONENT TESTS: tests/whitebox/test_tools.py
SCENARIO COVERAGE: all 21 (every tool-using scenario calls one of these)

Field-tag mapping conventions (referenced by policies/filter-rules.yaml):
  directory_basic    : name, department, title, office, manager
  directory_email    : work_email
  directory_phone    : work_phone
  personal_contact   : personal_email, personal_phone, home_address
  compensation       : salary, bonus_target
  performance        : performance_rating, last_review
  disciplinary       : disciplinary_actions
  employment_status  : employment_status
  drive_metadata     : drive_id, drive_type, owning_team, access_granted, expires
  account_type       : account_type
  temp_credential    : temp_password
  hr_policy_text     : result (HR policy mode)
  ticket_metadata    : ticket_id, estimated_response
"""
from __future__ import annotations

import secrets
import string
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

# -----------------------------------------------------------------------------
# Mock data store (small but covers the 21 scenarios)
# -----------------------------------------------------------------------------

# NAMES POLICY (important for reviewers):
#
# The problem statement names only four employees: Sarah Chen (EMP-1042), David
# Kim (her manager — EMP-1043 per scenario #16), Jordan Rivera (named in #13
# and #16, no EMP-ID given), and Jessica Park (referenced in #8 as the owner
# of a personal drive, no EMP-ID needed). It assigns EMP-IDs to several
# requesters (EMP-2011, EMP-1500, EMP-2200, EMP-3300, EMP-4010, EMP-5500) but
# never names them.
#
# This fixture therefore:
#   - Uses the canonical names for the four named employees.
#   - Assigns Jordan Rivera a fixture-supplied EMP-ID (EMP-1100) since the
#     lookup_employee tool's schema requires one; the name is canonical, only
#     the ID is fictional.
#   - Uses the placeholder string "Employee {EMP-ID}" as the `name` field for
#     every other requester record. No fabricated person names are introduced.
#   - Sets `manager` to None on placeholder records and on David Kim's record
#     (modelling David's own upline is not required by any scenario, and
#     inventing a name for it would reintroduce the same hallucination).
#
# Departments are taken verbatim from the problem statement's scenario
# annotations (e.g. scenario #4 explicitly says "Requester: EMP-1500,
# Marketing"). Personal fields (personal_email, salary, etc.) on placeholder
# records exist so the D2 filter and D11 leak detector have realistic shapes
# to operate on, but are never user-visible because the agent's response
# action does not reference the requester by name or any other PII.

# account_type ∈ {standard, admin, executive, service}
_ACCOUNTS: dict[str, dict[str, Any]] = {
    # ----- Canonical (named in problem statement) -----
    "EMP-1042": {
        "name": "Sarah Chen",
        "department": "Engineering",
        "title": "Senior Backend Engineer",
        "manager": "David Kim",
        "office": "Building 3, Floor 2",
        "work_email": "s.chen@gaggia.com",
        "work_phone": "x4521",
        "personal_email": "sarah.chen.personal@gmail.com",
        "personal_phone": "555-0147",
        "home_address": "742 Elm Street, Austin, TX",
        "salary": 158000,
        "bonus_target": "15%",
        "performance_rating": "Exceeds Expectations",
        "last_review": "2025-09-15",
        "disciplinary_actions": [],
        "employment_status": "Active",
        "account_type": "standard",
    },
    "EMP-1043": {
        "name": "David Kim",
        "department": "Engineering",
        "title": "Engineering Manager",
        # David's own upline is not modeled — no scenario requires it and
        # inventing one would reintroduce a hallucinated name.
        "manager": None,
        "office": "Building 3, Floor 2",
        "work_email": "d.kim@gaggia.com",
        "work_phone": "x4500",
        "personal_email": "d.kim.personal@example.test",
        "personal_phone": "555-0188",
        "home_address": "Address withheld, Austin, TX",
        "salary": 215000,
        "bonus_target": "20%",
        "performance_rating": "Meets Expectations",
        "last_review": "2025-08-22",
        "disciplinary_actions": [],
        "employment_status": "Active",
        "account_type": "standard",
        # Includes Sarah Chen, Jordan Rivera (EMP-1100), and the EMP-2200
        # placeholder. EMP-1500 (Marketing per problem statement) is NOT a
        # direct report.
        "direct_reports": ["EMP-1042", "EMP-1100", "EMP-2200"],
    },
    "EMP-1100": {
        # Jordan Rivera — named in scenarios #13 and #16. EMP-ID is
        # fixture-supplied because the lookup tool's schema requires one;
        # the name itself is canonical.
        "name": "Jordan Rivera",
        "department": "Engineering",
        "title": "Engineer II",
        "manager": "David Kim",
        "office": "Remote",
        "work_email": "j.rivera@gaggia.com",
        "work_phone": "x4533",
        "personal_email": "j.rivera.personal@example.test",
        "personal_phone": "555-0123",
        "home_address": "Address withheld, Boulder, CO",
        "salary": 132000,
        "bonus_target": "10%",
        "performance_rating": "Meets Expectations",
        "last_review": "2025-09-30",
        "disciplinary_actions": [],
        "employment_status": "Active",
        "account_type": "standard",
    },
    # ----- Placeholder requester records (problem statement assigns ID but no name) -----
    "EMP-1500": {
        # Marketing per problem statement scenario #4 ("Requester: EMP-1500,
        # Marketing"). Was previously mis-assigned to Engineering in the
        # original fixture; corrected here.
        "name": "Employee EMP-1500",
        "department": "Marketing",
        "title": "Marketing team member",
        "manager": None,
        "office": "Building 1, Floor 4",
        "work_email": "emp1500@gaggia.com",
        "work_phone": "x2215",
        "personal_email": "emp1500.personal@example.test",
        "personal_phone": "555-1500",
        "home_address": "Address withheld, Austin, TX",
        "salary": 98000,
        "bonus_target": "8%",
        "performance_rating": "Meets Expectations",
        "last_review": "2025-09-30",
        "disciplinary_actions": [],
        "employment_status": "Active",
        "account_type": "standard",
    },
    "EMP-3300": {
        "name": "Employee EMP-3300",
        "department": "Marketing",
        "title": "Marketing team member",
        "manager": None,
        "office": "Building 1, Floor 4",
        "work_email": "emp3300@gaggia.com",
        "work_phone": "x2210",
        "personal_email": "emp3300.personal@example.test",
        "personal_phone": "555-3300",
        "home_address": "Address withheld, Austin, TX",
        "salary": 92000,
        "bonus_target": "8%",
        "performance_rating": "Meets Expectations",
        "last_review": "2025-09-01",
        "disciplinary_actions": [],
        "employment_status": "Active",
        "account_type": "standard",
    },
    "EMP-2200": {
        # Engineering peer; reports to David Kim (so scenario #14's org-chart
        # answer includes this ID).
        "name": "Employee EMP-2200",
        "department": "Engineering",
        "title": "Engineer",
        "manager": "David Kim",
        "office": "Building 3, Floor 2",
        "work_email": "emp2200@gaggia.com",
        "work_phone": "x4540",
        "personal_email": "emp2200.personal@example.test",
        "personal_phone": "555-2200",
        "home_address": "Address withheld, Austin, TX",
        "salary": 110000,
        "bonus_target": "10%",
        "performance_rating": "Meets Expectations",
        "last_review": "2025-09-10",
        "disciplinary_actions": [],
        "employment_status": "Active",
        "account_type": "standard",
    },
    "EMP-4010": {
        "name": "Employee EMP-4010",
        "department": "DevOps",
        "title": "DevOps team member",
        "manager": None,
        "office": "Remote",
        "work_email": "emp4010@gaggia.com",
        "work_phone": "x6601",
        "personal_email": "emp4010.personal@example.test",
        "personal_phone": "555-4010",
        "home_address": "Address withheld, Seattle, WA",
        "salary": 175000,
        "bonus_target": "12%",
        "performance_rating": "Meets Expectations",
        "last_review": "2025-09-22",
        "disciplinary_actions": [],
        "employment_status": "Active",
        "account_type": "standard",
    },
    "EMP-2011": {
        "name": "Employee EMP-2011",
        "department": "Operations",
        "title": "Operations team member",
        "manager": None,
        "office": "Building 2, Floor 1",
        "work_email": "emp2011@gaggia.com",
        "work_phone": "x3304",
        "personal_email": "emp2011.personal@example.test",
        "personal_phone": "555-2011",
        "home_address": "Address withheld, Austin, TX",
        "salary": 78000,
        "bonus_target": "6%",
        "performance_rating": "Meets Expectations",
        "last_review": "2025-08-15",
        "disciplinary_actions": [],
        "employment_status": "Active",
        "account_type": "standard",
    },
    "EMP-5500": {
        "name": "Employee EMP-5500",
        "department": "Sales",
        "title": "Sales team member",
        "manager": None,
        "office": "Building 1, Floor 2",
        "work_email": "emp5500@gaggia.com",
        "work_phone": "x1102",
        "personal_email": "emp5500.personal@example.test",
        "personal_phone": "555-5500",
        "home_address": "Address withheld, Austin, TX",
        "salary": 145000,
        "bonus_target": "30%",
        "performance_rating": "Exceeds Expectations",
        "last_review": "2025-08-01",
        "disciplinary_actions": [],
        "employment_status": "Active",
        "account_type": "standard",
    },
    # Privileged accounts (must NOT be reset by the agent — §1.2)
    "svc-deploy": {
        "name": "Deploy Service Account",
        "department": "DevOps",
        "title": "Service Account",
        "manager": None,
        "office": None,
        "work_email": "svc-deploy@gaggia.com",
        "work_phone": None,
        "account_type": "service",
        "employment_status": "Active",
    },
    "sysadmin-01": {
        "name": "System Administrator 01",
        "department": "IT Security",
        "title": "Admin Account",
        "manager": None,
        "office": None,
        "work_email": "sysadmin-01@gaggia.com",
        "account_type": "admin",
        "employment_status": "Active",
    },
    "EMP-0001": {
        "name": "Former Employee",
        "department": "Engineering",
        "title": "Senior Engineer",
        "manager": None,
        "office": None,
        "work_email": "former@gaggia.com",
        "work_phone": None,
        "account_type": "standard",
        "employment_status": "Inactive",  # already offboarded
    },
}

# Drive registry. drive_type ∈ {team, cross-team, personal, restricted, legal-hold}
_DRIVES: dict[str, dict[str, Any]] = {
    "DRV-marketing-q3": {
        "drive_type": "team",
        "owning_team": "Marketing",
    },
    "DRV-marketing": {
        "drive_type": "team",
        "owning_team": "Marketing",
    },
    "DRV-design": {
        "drive_type": "team",
        "owning_team": "Design",
    },
    "DRV-finance-restricted": {
        "drive_type": "restricted",
        "owning_team": "Finance",
    },
    "DRV-legal-hold-2024-investigations": {
        "drive_type": "legal-hold",
        "owning_team": "Legal",
    },
    "DRV-jessica-park-personal": {
        "drive_type": "personal",
        "owning_team": None,
    },
}


# HR policy knowledge base for query_hr_database(query_type='policy').
_HR_POLICY_KB: list[tuple[tuple[str, ...], str]] = [
    (
        ("pto", "vacation", "time off"),
        "Gaggia employees receive 20 days PTO per year, accrued monthly. "
        "Unused PTO rolls over up to 5 days into the next calendar year. "
        "Managers approve PTO requests through the self-service portal.",
    ),
    (
        ("benefits", "enroll", "enrollment", "open enrollment"),
        "Open enrollment runs from October 15 to November 15 each year. "
        "Coverage takes effect January 1. New hires have a 30-day enrollment "
        "window from their start date.",
    ),
    (
        ("holiday", "holidays"),
        "Gaggia observes 11 paid holidays: New Year's Day, MLK Day, "
        "Presidents Day, Memorial Day, Juneteenth, Independence Day, "
        "Labor Day, Indigenous Peoples' Day, Thanksgiving, Day after "
        "Thanksgiving, and Christmas Day.",
    ),
    (
        ("parental", "leave", "maternity", "paternity"),
        "Gaggia offers 16 weeks of paid parental leave for primary caregivers "
        "and 8 weeks for secondary caregivers, available within 12 months of "
        "the birth or adoption.",
    ),
    (
        ("401k", "retirement", "match"),
        "Gaggia matches 401(k) contributions up to 5% of base salary, with "
        "immediate vesting. Contributions can be made pre-tax or Roth.",
    ),
]


def _gen_temp_password(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _now_ticket_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"ESC-{ts}-{secrets.randbelow(1000):03d}"


# -----------------------------------------------------------------------------
# Tool implementations
# -----------------------------------------------------------------------------


def reset_password(employee_id: str) -> dict[str, Any]:
    """Reset the password for an account. Returns full output (account_type
    + temp_password); the dispatcher's downstream filter applies §1.2 by
    refusing to surface temp_credential when the requester isn't self."""
    record = _ACCOUNTS.get(employee_id)
    if record is None:
        return {"status": "not_found", "employee_id": employee_id}
    return {
        "status": "success",
        "employee_id": employee_id,
        "account_type": record.get("account_type", "standard"),
        "temp_password": _gen_temp_password(),
        "expires_in": "24 hours",
    }


def lookup_employee(query: str) -> dict[str, Any]:
    """Look up by employee_id or by name (case-insensitive substring).
    Returns the FULL profile — the filter (D2) strips fields the requester
    isn't authorized to see."""
    if query in _ACCOUNTS:
        record = _ACCOUNTS[query]
        return {"employee_id": query, **record}
    q = query.lower()
    for emp_id, record in _ACCOUNTS.items():
        if record.get("name", "").lower().find(q) >= 0:
            return {"employee_id": emp_id, **record}
    return {"status": "not_found", "query": query}


def grant_file_access(
    employee_id: str,
    drive_id: str,
    access_level: str,
    duration_days: int | None = None,
) -> dict[str, Any]:
    """Grant access to a drive. Returns drive metadata even when access is
    granted at the tool level — the agent + filter still need to enforce
    §3.3 (must not grant access to restricted/legal-hold) and §3.4 (must
    not grant access to personal drives)."""
    drive = _DRIVES.get(drive_id)
    if drive is None:
        return {"status": "not_found", "drive_id": drive_id}
    expires = None
    if duration_days is not None:
        expires = f"+{duration_days} days from now"
    return {
        "status": "success",
        "drive_id": drive_id,
        "drive_type": drive["drive_type"],
        "owning_team": drive["owning_team"],
        "access_granted": access_level,
        "expires": expires,
    }


def query_hr_database(query_type: str, employee_id: str | None = None) -> dict[str, Any]:
    """Two modes:
      - policy: free-text answer from a small canned KB (allowed broadly).
        The `employee_id` arg, when query_type=policy, is overloaded as
        a keyword hint ("pto", "benefits", etc.). If it doesn't match any
        keyword (e.g. agent passed None), we return the FULL KB as a
        catalog so the synthesis pass can find the right entry. This is
        more agent-friendly than returning "not available".
      - individual: full HR record — compensation, performance, disciplinary,
        employment_status. The filter (D2) strips by tag.
    """
    if query_type == "policy":
        kb_query = (employee_id or "").lower()
        # First: exact keyword match.
        for keywords, answer in _HR_POLICY_KB:
            if kb_query and any(k in kb_query for k in keywords):
                return {"query_type": "policy", "result": answer}
        # Fallback: return the full KB so the agent can pick the relevant
        # entry during the synthesis pass. Realistic since a real HR KB
        # would also support full-text retrieval.
        catalog = "\n\n".join(
            f"Topic: {', '.join(keywords)}\n{answer}"
            for keywords, answer in _HR_POLICY_KB
        )
        return {
            "query_type": "policy",
            "result": (
                "Full HR policy knowledge base (pick the relevant entry):\n\n"
                + catalog
            ),
        }
    if query_type == "individual":
        record = _ACCOUNTS.get(employee_id or "")
        if record is None:
            return {"status": "not_found", "employee_id": employee_id}
        return {
            "query_type": "individual",
            "employee_id": employee_id,
            "salary": record.get("salary"),
            "bonus_target": record.get("bonus_target"),
            "last_review": record.get("last_review"),
            "performance_rating": record.get("performance_rating"),
            "disciplinary_actions": record.get("disciplinary_actions", []),
            "employment_status": record.get("employment_status"),
        }
    return {
        "status": "error",
        "message": f"unknown query_type: {query_type!r} (expected 'policy' or 'individual')",
    }


def escalate_to_human(reason: str, conversation_summary: str) -> dict[str, Any]:
    return {
        "status": "escalated",
        "ticket_id": _now_ticket_id(),
        "estimated_response": "2 hours",
    }


# -----------------------------------------------------------------------------
# Tool registry — declarative shape; keeps tags out of the function bodies
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolArg:
    name: str
    type: str           # "str" | "int" | "str|None" | etc.
    description: str
    required: bool = True


@dataclass(frozen=True)
class ToolSchema:
    """Per-tool declaration consumed by the registry, dispatcher, and filter."""

    name: str
    callable: Callable[..., dict[str, Any]]
    description: str
    args: tuple[ToolArg, ...] = ()
    response_field_tags: dict[str, str] = field(default_factory=dict)
    untagged_fields: tuple[str, ...] = ()  # fields that are operational, not PII


_REGISTRY: dict[str, ToolSchema] = {
    "reset_password": ToolSchema(
        name="reset_password",
        callable=reset_password,
        description="Reset an employee's account password.",
        args=(
            ToolArg("employee_id", "str", "The employee ID whose password to reset, e.g. 'EMP-2011' or 'svc-deploy'."),
        ),
        response_field_tags={
            "account_type": "account_type",
            "temp_password": "temp_credential",
        },
        untagged_fields=("status", "employee_id", "expires_in"),
    ),
    "lookup_employee": ToolSchema(
        name="lookup_employee",
        callable=lookup_employee,
        description="Look up an employee's profile by name or ID.",
        args=(
            ToolArg("query", "str", "Employee ID (e.g. 'EMP-1042') or substring of the employee's name (e.g. 'Sarah Chen')."),
        ),
        response_field_tags={
            "name": "directory_basic",
            "department": "directory_basic",
            "title": "directory_basic",
            "office": "directory_basic",
            "manager": "directory_basic",
            "work_email": "directory_email",
            "work_phone": "directory_phone",
            "personal_email": "personal_contact",
            "personal_phone": "personal_contact",
            "home_address": "personal_contact",
            "salary": "compensation",
            "bonus_target": "compensation",
            "performance_rating": "performance",
            "last_review": "performance",
            "disciplinary_actions": "disciplinary",
            "employment_status": "employment_status",
            "direct_reports": "directory_basic",
            "account_type": "account_type",
        },
        untagged_fields=("status", "query", "employee_id"),
    ),
    "grant_file_access": ToolSchema(
        name="grant_file_access",
        callable=grant_file_access,
        description="Grant access to a shared drive.",
        args=(
            ToolArg("employee_id", "str", "The employee ID receiving access."),
            ToolArg("drive_id", "str", "The drive identifier, e.g. 'DRV-marketing-q3' or 'DRV-design'."),
            ToolArg("access_level", "str", "One of: 'read', 'write', 'admin'."),
            ToolArg("duration_days", "int|None", "Number of days for temporary access (per §3.2 max 7); null for permanent.", required=False),
        ),
        response_field_tags={
            "drive_id": "drive_metadata",
            "drive_type": "drive_metadata",
            "owning_team": "drive_metadata",
            "access_granted": "drive_metadata",
            "expires": "drive_metadata",
        },
        untagged_fields=("status",),
    ),
    "query_hr_database": ToolSchema(
        name="query_hr_database",
        callable=query_hr_database,
        description="Query HR policies (query_type='policy') or an "
        "individual employee record (query_type='individual').",
        args=(
            ToolArg("query_type", "str", "Either 'policy' for general HR knowledge-base questions, or 'individual' for a specific employee's compensation/performance/disciplinary record."),
            ToolArg("employee_id", "str|None", "For query_type='individual': the employee ID. For query_type='policy': pass keywords from the question (e.g. 'pto', 'benefits') and the canned KB will look them up.", required=False),
        ),
        response_field_tags={
            "result": "hr_policy_text",
            "salary": "compensation",
            "bonus_target": "compensation",
            "last_review": "performance",
            "performance_rating": "performance",
            "disciplinary_actions": "disciplinary",
            "employment_status": "employment_status",
        },
        untagged_fields=("query_type", "status", "employee_id", "message"),
    ),
    "escalate_to_human": ToolSchema(
        name="escalate_to_human",
        callable=escalate_to_human,
        description="Escalate the conversation to a human IT operator.",
        args=(
            ToolArg("reason", "str", "Why this is being escalated."),
            ToolArg("conversation_summary", "str", "Per §5.4: a summary of the conversation for the human operator."),
        ),
        response_field_tags={
            "ticket_id": "ticket_metadata",
            "estimated_response": "ticket_metadata",
        },
        untagged_fields=("status",),
    ),
}


def get_tool(name: str) -> ToolSchema:
    if name not in _REGISTRY:
        raise KeyError(f"unknown tool: {name}")
    return _REGISTRY[name]


def list_tools() -> list[ToolSchema]:
    return list(_REGISTRY.values())


def tool_specs_for_prompt() -> list[dict[str, Any]]:
    """Compact tool spec list for inclusion in the agent's system prompt.
    Includes argument names and types so the agent uses them correctly
    (the dispatcher rejects calls with the wrong arg shape)."""
    out: list[dict[str, Any]] = []
    for t in _REGISTRY.values():
        out.append({
            "name": t.name,
            "description": t.description,
            "args": [
                {
                    "name": a.name,
                    "type": a.type,
                    "description": a.description,
                    "required": a.required,
                }
                for a in t.args
            ],
        })
    return out


if __name__ == "__main__":
    print(f"{len(_REGISTRY)} tools registered:")
    for spec in _REGISTRY.values():
        print(f"  - {spec.name}: {spec.description}")
        for field_name, tag in spec.response_field_tags.items():
            print(f"      {field_name} -> {tag}")
        if spec.untagged_fields:
            print(f"      (untagged: {', '.join(spec.untagged_fields)})")
