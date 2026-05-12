"""
COMPONENT: ui
DESIGN-REF: v2 (Gradio chat UI surface)
PURPOSE: Demoable chat surface over the orchestrator. Sidebar selects
  tier (Blue/Grey/Red) and requester employee_id; the main panel shows
  the user-facing response on top and a collapsible "Agent internals"
  panel below with decision, citations, tool_calls (dispatch + filter
  state), pipeline_status, repair_attempts (D13 drift kinds + feedback),
  cost_assessment (Grey), CoVe verdict, Prompt Guard 2 verdict, and
  leak_detection events. When TRACING_ENABLED=true, a Phoenix UI link
  is shown.
PROBLEM-STATEMENT REQ (verbatim): >
  (No explicit UI requirement in the brief — this is a demoability
  add-on for v2. It exposes the orchestrator's structured response so
  reviewers can exercise scenarios interactively.)
EXPECTED INPUT: user message in the chat box + sidebar selections
EXPECTED OUTPUT: rendered chat reply + internals JSON-ish panel
UPSTREAM: invoked manually via `python -m policy_agent.ui`
DOWNSTREAM: gradio, policy_agent.orchestrator
COMPONENT TESTS: manual; no automated test (UI surface).
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import gradio as gr
import yaml

from policy_agent.orchestrator import OrchestratorResult, handle_request

REPO_ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_YAML = REPO_ROOT / "tests" / "scenarios.yaml"

# Sentinel value for the "type your own" / no preselect option.
_NO_SCENARIO = "(none — type your own)"


# Preset employee IDs from the mock data (see policy_agent/tools.py).
#
# Only Sarah Chen, David Kim, and Jordan Rivera are named in the problem
# statement; the remaining requesters carry placeholder labels rather than
# fabricated names. Jordan Rivera's EMP-ID is fixture-supplied (EMP-1100)
# because the problem statement names her without an ID.
_EMPLOYEE_PRESETS = [
    ("(none)", ""),
    ("EMP-1042 Sarah Chen (Engineering)", "EMP-1042"),
    ("EMP-1043 David Kim (Engineering Manager — direct reports include EMP-1042/1100/2200)", "EMP-1043"),
    ("EMP-1100 Jordan Rivera (Engineering, reports to David Kim)", "EMP-1100"),
    ("EMP-1500 (Marketing requester)", "EMP-1500"),
    ("EMP-2011 (Operations requester)", "EMP-2011"),
    ("EMP-2200 (Engineering requester, reports to David Kim)", "EMP-2200"),
    ("EMP-3300 (Marketing requester)", "EMP-3300"),
    ("EMP-4010 (DevOps requester)", "EMP-4010"),
    ("EMP-5500 (Sales requester)", "EMP-5500"),
]


def _emp_label_to_id(label: str) -> str | None:
    for lbl, eid in _EMPLOYEE_PRESETS:
        if lbl == label:
            return eid or None
    # Unknown label → assume user typed a custom ID
    return label.strip() or None


# ---------------------------------------------------------------------------
# Scenario preselects — load the 21 declared scenarios for one-click demos
# ---------------------------------------------------------------------------

def _load_scenarios() -> list[dict]:
    """Load tests/scenarios.yaml at UI startup. Returns the declared 21
    plus any LLM-generated extras (if scenarios_generated.yaml exists
    they could also be merged — v3, not needed for the demo)."""
    try:
        raw = yaml.safe_load(SCENARIOS_YAML.read_text())
        return list(raw.get("scenarios", []))
    except Exception as exc:
        print(f"[ui] could not load scenarios: {exc}")
        return []


def _scenario_choices() -> list[str]:
    """Build the dropdown labels: '#<id> [<tier>] — <message preview>'.

    Returns a list of label strings; the sentinel _NO_SCENARIO is always
    first so the user can deselect / type their own.
    """
    choices = [_NO_SCENARIO]
    for s in _load_scenarios():
        sid = s.get("id", "?")
        tier = s.get("tier", "?")
        msg = (s.get("message") or "").strip()
        preview = msg[:60] + ("..." if len(msg) > 60 else "")
        choices.append(f"#{sid} [{tier}] — {preview}")
    return choices


def _scenario_by_label(label: str) -> dict | None:
    if not label or label == _NO_SCENARIO:
        return None
    # Parse the leading "#<id>" off the label.
    try:
        sid = int(label.split()[0].lstrip("#"))
    except (ValueError, IndexError):
        return None
    for s in _load_scenarios():
        if s.get("id") == sid:
            return s
    return None


def _emp_id_to_label(emp_id: str | None) -> str:
    """Reverse-lookup an employee_id to its preset label, or `(none)` if
    not in the preset list."""
    if not emp_id:
        return _EMPLOYEE_PRESETS[0][0]   # "(none)"
    for lbl, eid in _EMPLOYEE_PRESETS:
        if eid == emp_id:
            return lbl
    return _EMPLOYEE_PRESETS[0][0]


def _on_scenario_pick(label: str) -> tuple[str, str, str, str]:
    """Dropdown .change() handler.

    Returns (message_text, tier_value, emp_preset_label, custom_employee_id).
    Updates ALL of msg_in, tier_in, emp_in, AND emp_custom so the user
    sees the full request context change when they pick a new scenario
    (the prior version only updated emp_custom, leaving the preset
    dropdown displaying a misleading stale employee).
    """
    scenario = _scenario_by_label(label)
    if scenario is None:
        # Reset to defaults.
        return "", "Blue", _EMPLOYEE_PRESETS[0][0], ""
    emp_id = scenario.get("requester_employee_id") or ""
    return (
        scenario.get("message", ""),
        scenario.get("tier", "Blue"),
        _emp_id_to_label(emp_id),
        emp_id,
    )


def _render_internals(o: OrchestratorResult) -> str:
    """Format the orchestrator's internal state as a single markdown blob
    for the right-hand panel."""
    lines: list[str] = []
    lines.append(f"**Path:** {o.architectural_path}")
    lines.append(f"**Pipeline status:** `{o.pipeline_status}`")
    lines.append(f"**Decision:** `{o.response.decision}`")
    # Phoenix deep link for THIS request's trace, if tracing is enabled.
    if o.trace_id:
        port = os.environ.get("PHOENIX_PORT", "6006")
        # Phoenix's primary spans view, scoped to the policy-agent project
        # and filtered by this request's trace_id. Most recent runs are at
        # the top of the list; the user's spans appear under the top-level
        # "Request [<Tier>]: ..." human-readable span.
        phoenix_url = f"http://localhost:{port}/projects/policy-agent/traces/{o.trace_id}"
        lines.append(f"**Phoenix trace:** [open this request in Phoenix]({phoenix_url})")
    lines.append("")
    if o.response.citations:
        lines.append("**Citations**")
        for c in o.response.citations:
            qt = c.quote[:200] + ("..." if len(c.quote) > 200 else "")
            lines.append(f"- §{c.section_id}: {qt}")
        lines.append("")
    if o.tool_executions:
        lines.append("**Tool calls**")
        for ex in o.tool_executions:
            args_json = json.dumps(ex.proposed["args"])
            lines.append(
                f"- proposed `{ex.proposed['name']}({args_json})` → "
                f"dispatch=**{ex.dispatch.status}**"
            )
            if ex.dispatch.rejection_reason:
                lines.append(f"   - rejection: {ex.dispatch.rejection_reason}")
            if ex.filter is not None:
                kept = sorted(ex.filter.filtered_output.keys())
                redacted = sorted(ex.filter.redacted_fields)
                lines.append(
                    f"   - filter (rel=`{ex.relationship}`): kept={kept}; redacted={redacted}"
                )
        lines.append("")
    if o.response.cost_assessment:
        ca = o.response.cost_assessment
        lines.append("**Cost assessment (Grey)**")
        lines.append(
            f"- harm_act=`{ca.harm_if_acted_wrongly}` "
            f"harm_refuse=`{ca.harm_if_refused_wrongly}` "
            f"reversibility=`{ca.reversibility}` "
            f"affects=`{ca.affects}` "
            f"chosen=`{ca.chosen_path}`"
        )
        lines.append(f"- justification: {ca.justification}")
        lines.append("")
    if o.repair_attempts:
        lines.append("**D13 repair attempts**")
        for ra in o.repair_attempts:
            lines.append(
                f"- attempt {ra.attempt_index}: drifts={ra.drift_kinds}"
            )
        lines.append("")
    if o.prompt_guard_verdict:
        pg = o.prompt_guard_verdict
        lines.append(
            f"**Prompt Guard 2** — is_injection=`{pg.is_injection}` "
            f"score=`{pg.score:.2f}` method=`{pg.method}`"
        )
    if o.cove_verdict and o.cove_verdict.invoked:
        cv = o.cove_verdict
        lines.append(
            f"**CoVe** — aligned=`{cv.aligned}` questions=`{len(cv.questions)}` "
            f"divergences=`{len(cv.divergences)}`"
        )
        if cv.divergences:
            for d in cv.divergences[:3]:
                lines.append(f"  - {d[:200]}")
    if o.leak_detection and o.leak_detection.invoked:
        ld = o.leak_detection
        lines.append(
            f"**Leak detector** — events=`{len(ld.events)}` clean=`{ld.clean}`"
        )
        for ev in ld.events:
            lines.append(
                f"  - LEAK: type=`{ev.entity_type}` span=`{ev.span}` field=`{ev.source_field}`"
            )
    lines.append("")
    lines.append("**Reasoning**")
    lines.append(o.response.reasoning)
    return "\n".join(lines)


def _phoenix_link() -> str:
    truthy = ("1", "true", "yes")
    enabled = (
        os.environ.get("TRACING_ENABLED", "false").lower() in truthy
        or os.environ.get("PHOENIX_ENABLED", "false").lower() in truthy
    )
    if enabled:
        port = os.environ.get("PHOENIX_PORT", "6006")
        return f"Phoenix trace UI: http://localhost:{port}"
    return "Phoenix tracing is OFF — set `TRACING_ENABLED=true` (or legacy `PHOENIX_ENABLED=true`) and restart to enable."


def _on_submit(
    message: str,
    history: list[dict[str, str]],
    tier: str,
    employee_label: str,
    custom_employee_id: str,
) -> tuple[list[dict[str, str]], str, str]:
    """Process one chat turn. Returns (updated history, internals markdown, status)."""
    msg = (message or "").strip()
    if not msg:
        return history, "", "(no message)"

    emp = custom_employee_id.strip() or _emp_label_to_id(employee_label)
    request_id = f"ui-{uuid.uuid4().hex[:8]}"

    try:
        result = handle_request(
            user_message=msg,
            tier=tier,
            employee_id=emp,
            request_id=request_id,
        )
    except Exception as exc:
        history = list(history) + [
            {"role": "user", "content": msg},
            {"role": "assistant", "content": f"[error] {type(exc).__name__}: {exc}"},
        ]
        return history, f"**ERROR**\n```\n{exc}\n```", "error"

    reply = result.response.format_for_user()
    history = list(history) + [
        {"role": "user", "content": msg},
        {"role": "assistant", "content": reply},
    ]
    internals = _render_internals(result)
    status = (
        f"decision=`{result.response.decision}` | "
        f"pipeline=`{result.pipeline_status}` | "
        f"repairs={len(result.repair_attempts)}"
    )
    return history, internals, status


def build_app() -> gr.Blocks:
    with gr.Blocks(title="Gaggia IT Helpdesk Agent", fill_height=True) as app:
        gr.Markdown(
            "# Gaggia IT Helpdesk Policy Agent\n"
            "Operates strictly within a written policy. Tier and employee_id "
            "are upstream-supplied context (no UI inference)."
        )
        with gr.Row():
            with gr.Column(scale=2):
                chatbot = gr.Chatbot(
                    height=500,
                    show_label=False,
                )
                with gr.Row():
                    msg_in = gr.Textbox(
                        show_label=False,
                        placeholder="Type a request (e.g. 'I forgot my password')...",
                        scale=4,
                    )
                    submit_btn = gr.Button("Send", variant="primary", scale=1)
                status_md = gr.Markdown("")
                gr.Markdown(f"_{_phoenix_link()}_")
            with gr.Column(scale=2):
                gr.Markdown("### Quick load: problem-statement scenarios")
                scenario_in = gr.Dropdown(
                    choices=_scenario_choices(),
                    value=_NO_SCENARIO,
                    label="Load scenario from tests/scenarios.yaml",
                    info=(
                        "Populates the message, tier, and employee_id for one of "
                        "the 21 declared scenarios. Edit before sending if you want to "
                        "tweak the wording."
                    ),
                )
                gr.Markdown("### Request context")
                tier_in = gr.Radio(
                    choices=["Blue", "Grey", "Red"],
                    value="Blue",
                    label="Trust tier",
                    info="Blue = identity verified; Grey = ambiguous; Red = untrusted",
                )
                emp_in = gr.Dropdown(
                    choices=[lbl for lbl, _ in _EMPLOYEE_PRESETS],
                    value=_EMPLOYEE_PRESETS[1][0],
                    label="Requester employee ID (preset)",
                )
                emp_custom = gr.Textbox(
                    label="...or custom employee_id",
                    placeholder="(blank to use preset above)",
                )
                gr.Markdown("### Agent internals (read-only)")
                internals_md = gr.Markdown("")

        # Scenario preselect → populate message, tier, preset employee
        # dropdown, AND custom employee_id (Fix D — keeps emp_in display
        # in sync with the chosen scenario so reviewers don't see a stale
        # preset label).
        scenario_in.change(
            _on_scenario_pick,
            inputs=[scenario_in],
            outputs=[msg_in, tier_in, emp_in, emp_custom],
        )

        submit_btn.click(
            _on_submit,
            inputs=[msg_in, chatbot, tier_in, emp_in, emp_custom],
            outputs=[chatbot, internals_md, status_md],
        ).then(lambda: "", outputs=[msg_in])
        msg_in.submit(
            _on_submit,
            inputs=[msg_in, chatbot, tier_in, emp_in, emp_custom],
            outputs=[chatbot, internals_md, status_md],
        ).then(lambda: "", outputs=[msg_in])

    return app


def main() -> None:
    app = build_app()
    port = int(os.environ.get("UI_PORT", "7860"))
    app.launch(server_port=port, share=False)


if __name__ == "__main__":
    main()
