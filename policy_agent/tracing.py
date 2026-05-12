"""
COMPONENT: tracing
DESIGN-REF: D10 (Decision logging via OpenTelemetry GenAI + Arize Phoenix)
PURPOSE: Optional OpenTelemetry-spans-into-Phoenix observability layer.
  Each numbered step in the orchestrator pipeline emits a span with
  OTel GenAI semconv attributes plus a few policy-agent-specific ones
  (tier, requester_employee_id, decision, pipeline_status, drift_kinds,
  ...). Phoenix runs in-process so reviewers see traces locally with no
  signup, no docker.
PROBLEM-STATEMENT REQ (verbatim): >
  "Decision Logging — Is the agent's reasoning inspectable? Can you trace
  why it made a specific decision after the fact?"
EXPECTED INPUT: imports / env config
EXPECTED OUTPUT: a `tracer` + `span()` context-manager helper
UPSTREAM: orchestrator, agent, red_path, dispatcher, filter, citation_verifier,
  consistency_reviewer, cove, leak_detector, prompt_guard
DOWNSTREAM: opentelemetry-sdk, arize-phoenix

Design notes:
- **Opt-in via env**: `TRACING_ENABLED` (true/false). Off → `span()` is a
  no-op context manager. This keeps the eval fast when not debugging.
- **Idempotent init**: `init_tracing()` may be called multiple times; only
  the first call wires up Phoenix + OTel.
- **GenAI semconv** attributes used where natural (gen_ai.system,
  gen_ai.request.model, gen_ai.usage.*). Custom attributes use the
  `policy_agent.*` namespace.
"""
from __future__ import annotations

import contextlib
import os
import threading
from typing import Any, Iterator

# We import OpenTelemetry lazily inside init_tracing() so a TRACING_ENABLED=false
# run doesn't pay the import cost.

_INIT_LOCK = threading.Lock()
_INITIALIZED = False
_TRACER: Any = None              # opentelemetry.trace.Tracer | None
_PHOENIX_SESSION: Any = None     # phoenix.Session | None
_NOOP_TRACER: Any = None         # no-op fallback


def _tracing_enabled() -> bool:
    """Tracing is on if either of these env vars is truthy:

    - `TRACING_ENABLED` (canonical, documented)
    - `PHOENIX_ENABLED` (legacy alias used in earlier .env.example files)

    Loads `.env` first since `init_tracing()` may run at module import
    time before any other component has called `load_dotenv()`.
    """
    _load_dotenv_once()
    truthy = ("1", "true", "yes")
    return (
        os.environ.get("TRACING_ENABLED", "false").lower() in truthy
        or os.environ.get("PHOENIX_ENABLED", "false").lower() in truthy
    )


_DOTENV_LOADED = False


def _load_dotenv_once() -> None:
    """Load REPO_ROOT/.env at most once. Safe to call from
    `init_tracing()` which may fire before any other module has loaded
    the .env file."""
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    try:
        from pathlib import Path
        from dotenv import load_dotenv

        repo_root = Path(__file__).resolve().parent.parent
        env_path = repo_root / ".env"
        if env_path.exists():
            load_dotenv(env_path)
        _DOTENV_LOADED = True
    except Exception:
        _DOTENV_LOADED = True


def _phoenix_port() -> int:
    return int(os.environ.get("PHOENIX_PORT", "6006"))


def _wait_for_listener(host: str, port: int, max_wait_s: float = 2.0) -> bool:
    """Poll a TCP connect to (host, port) until it succeeds or the deadline
    elapses. Used to confirm Phoenix is actually reachable before wiring up
    the OTel exporter — without this probe, init_tracing() would happily
    register an exporter against a dead endpoint, producing endless
    "Connection refused" log spam at runtime when Phoenix fails to start
    or has already died.
    """
    import socket
    import time

    deadline = time.monotonic() + max_wait_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def init_tracing(*, project_name: str = "policy-agent") -> None:
    """Initialize OTel + Phoenix. Idempotent. No-op if TRACING_ENABLED is false."""
    global _INITIALIZED, _TRACER, _PHOENIX_SESSION
    if _INITIALIZED:
        return
    with _INIT_LOCK:
        if _INITIALIZED:
            return
        if not _tracing_enabled():
            _INITIALIZED = True
            return
        try:
            import phoenix as px
            from phoenix.otel import register

            # Try to launch an in-process Phoenix. A non-fatal exception
            # here usually means another Phoenix is already bound to the
            # port (which we can attach to) — but it may also mean Phoenix
            # genuinely failed (e.g. crashed dependency). The probe below
            # is what tells the two apart.
            try:
                _PHOENIX_SESSION = px.launch_app(port=_phoenix_port())
            except Exception as exc:
                print(
                    f"[tracing] phoenix launch raised on :{_phoenix_port()} ({exc}); "
                    f"checking for an existing instance..."
                )
                _PHOENIX_SESSION = None

            # Confirm Phoenix is actually reachable before registering the
            # OTel exporter. If we skip this probe and Phoenix isn't up,
            # every span export will fail with "Connection refused" until
            # the process exits.
            if not _wait_for_listener("127.0.0.1", _phoenix_port()):
                print(
                    f"[tracing] phoenix not reachable on :{_phoenix_port()}; "
                    f"continuing without tracing. Agent still works; only "
                    f"the Phoenix trace UI is disabled."
                )
                _INITIALIZED = True
                _TRACER = None
                return

            # Register OTel TracerProvider pointing at Phoenix's OTLP endpoint.
            tracer_provider = register(
                project_name=project_name,
                endpoint=f"http://localhost:{_phoenix_port()}/v1/traces",
                set_global_tracer_provider=True,
            )
            _TRACER = tracer_provider.get_tracer(project_name)
            _INITIALIZED = True
            print(f"[tracing] enabled; phoenix at http://localhost:{_phoenix_port()}")
        except Exception as exc:
            # Tracing must NEVER break the agent. Log and continue with no-op.
            print(f"[tracing] init failed; continuing without tracing: {exc}")
            _INITIALIZED = True
            _TRACER = None


def get_tracer() -> Any:
    """Return the active tracer, or the no-op fallback."""
    if not _INITIALIZED:
        init_tracing()
    if _TRACER is not None:
        return _TRACER
    global _NOOP_TRACER
    if _NOOP_TRACER is None:
        from opentelemetry.trace import NoOpTracer
        _NOOP_TRACER = NoOpTracer()
    return _NOOP_TRACER


@contextlib.contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """Context manager that opens a named span with attributes.

    Attribute conventions:
      - OTel GenAI semconv keys where applicable: `gen_ai.system`,
        `gen_ai.request.model`, `gen_ai.usage.*`, `gen_ai.response.id`.
      - Policy-agent-specific: `policy_agent.tier`,
        `policy_agent.requester_employee_id`, `policy_agent.decision`,
        `policy_agent.pipeline_status`, `policy_agent.drift_kinds`, ...

    Usage:
        with span("agent.run", **{"policy_agent.tier": "Blue"}) as sp:
            ...
            sp.set_attribute("policy_agent.decision", "allow")
    """
    tracer = get_tracer()
    with tracer.start_as_current_span(name) as sp:
        try:
            for k, v in attributes.items():
                if v is None:
                    continue
                # OTel requires primitive or list-of-primitive values.
                if isinstance(v, (str, int, float, bool)):
                    sp.set_attribute(k, v)
                else:
                    sp.set_attribute(k, str(v))
        except Exception:
            pass
        yield sp


def current_trace_id_hex() -> str | None:
    """Return the active span's trace_id as a 32-char hex string, or
    None if there's no active span. Used by orchestrator to surface a
    Phoenix deep link per request."""
    try:
        from opentelemetry.trace import get_current_span
        sp = get_current_span()
        if sp is None:
            return None
        ctx = sp.get_span_context()
        if ctx is None or ctx.trace_id == 0:
            return None
        return f"{ctx.trace_id:032x}"
    except Exception:
        return None


def set_span_attributes(**attributes: Any) -> None:
    """Set attributes on the current active span (if any)."""
    try:
        from opentelemetry.trace import get_current_span
        sp = get_current_span()
        if sp is None:
            return
        for k, v in attributes.items():
            if v is None:
                continue
            if isinstance(v, (str, int, float, bool)):
                sp.set_attribute(k, v)
            else:
                sp.set_attribute(k, str(v))
    except Exception:
        pass
